from __future__ import annotations

import asyncio
import functools
import json
import threading
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Union
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.context import attach, detach, set_value
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter, SpanExportResult
from termcolor import colored

from .config import Configuration
from .event import ErrorEvent, Event
from .exceptions import ApiServerException
from .helpers import filter_unjsonable, get_ISO_time, safe_serialize
from .http_client import HttpClient, Response
from .log_config import logger
from .database import DatabaseManager
from .clerk import ClerkManager
from .token import generate_jwt_token

import os
import time
from litellm import cost_per_token

"""
OTEL Guidelines:



- Maintain a single TracerProvider for the application runtime
    - Have one global TracerProvider in the Client class

- According to the OpenTelemetry Python documentation, Resource should be initialized once per application and shared across all telemetry (traces, metrics, logs).
- Each Session gets its own Tracer (with session-specific context)
- Allow multiple sessions to share the provider while maintaining their own context



:: Resource

    ''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''
    Captures information about the entity producing telemetry as Attributes.
    For example, a process producing telemetry that is running in a container
    on Kubernetes has a process name, a pod name, a namespace, and possibly
    a deployment name. All these attributes can be included in the Resource.
    ''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''

    The key insight from the documentation is:

    - Resource represents the entity producing telemetry - in our case, that's the AgentOps SDK application itself
    - Session-specific information should be attributes on the spans themselves
        - A Resource is meant to identify the service/process/application1
        - Sessions are units of work within that application
        - The documentation example about "process name, pod name, namespace" refers to where the code is running, not the work it's doing

"""

clerk_secret_key = os.getenv("CLERK_SECRET_KEY")
clerk_publishable_key = os.getenv("CLERK_PUBLISHABLE_KEY")
clerk_test_user = os.getenv("CLERK_TEST_USER")
clerk_frontend_url = os.getenv("CLERK_FRONTEND_URL")
clerk_api_base_url = os.getenv("CLERK_API_BASE_URL")

if not clerk_secret_key or not clerk_publishable_key or not clerk_test_user or not clerk_frontend_url or not clerk_api_base_url:
    try:
        # Initialize Clerk  Manager with SSM parameter names
        clerk_manager = ClerkManager(
            clerk_secret_key_param_name=os.getenv('CLERK_SECRET_KEY_PARAM_NAME'),
            clerk_test_user_param_name=os.getenv('CLERK_USER_ID_PARAM_NAME'),
            clerk_api_base_url_param_name=os.getenv('CLERK_API_BASE_URL_PARAM_NAME'),
            clerk_publishable_key_param_name=os.getenv('CLERK_PUBLISHABLE_KEY_PARAM_NAME'),
            clerk_frontend_url_param_name=os.getenv('CLERK_FRONTEND_URL_PARAM_NAME')
        )
        
        # Retrieve all Clerk-related variables
        clerk_variables = clerk_manager.get_all_clerk_variables()  

        # Unpack variables
        clerk_secret_key, clerk_test_user, clerk_api_base_url, clerk_publishable_key, clerk_frontend_url = clerk_variables
        
    except Exception as e:
        print(f"Error: {e}")





class AgentStatus(Enum):
    """Enum representing possible states of an agent."""
    NOT_USED = "not_used"
    IN_PROGRESS = "in_progress" 
    COMPLETED = "completed"
    FAILED = "failed"

class AgentStatusTracker:
    """Manages the status and lifecycle of agents within a session."""
    
    def __init__(self):
        self._agents: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        
    def add_agent(self, agent_id: str, name: str) -> None:
        """Add a new agent with initial NOT_USED status."""
        with self._lock:
            if agent_id not in self._agents:
                self._agents[agent_id] = {
                    "name": name,
                    "status": AgentStatus.NOT_USED,
                    "last_updated": datetime.now(timezone.utc).isoformat()
                }
        
    def update_status(self, agent_id: str, status: AgentStatus) -> bool:
        """Update the status of an agent."""
        with self._lock:
            if agent_id not in self._agents:
                logger.warning(f"Attempted to update non-existent agent: {agent_id}")
                return False
                
            self._agents[agent_id].update({
                "status": status,
                "last_updated": datetime.now(timezone.utc).isoformat()
            })
            return True
            
    def get_agent_status(self, agent_id: str) -> Optional[AgentStatus]:
        """Get the current status of an agent."""
        with self._lock:
            agent_data = self._agents.get(agent_id)
            return agent_data["status"] if agent_data else None

    def get_agent_data(self, agent_id: str) -> Optional[Dict]:
        """Get all data for an agent."""
        with self._lock:
            return self._agents.get(agent_id)

    def get_active_agent(self) -> Optional[str]:
        """Get the ID of the current active (IN_PROGRESS) agent."""
        with self._lock:
            for agent_id, data in self._agents.items():
                if data["status"] == AgentStatus.IN_PROGRESS:
                    return agent_id
        return None

class SessionStatus(Enum):
    """Enum representing possible states of a session."""
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class EndState(Enum):
    """
    Enum representing the possible end states of a session.

    Attributes:
        SUCCESS: Indicates the session ended successfully.
        FAIL: Indicates the session failed.
        INDETERMINATE (default): Indicates the session ended with an indeterminate state.
                       This is the default state if not specified, e.g. if you forget to call end_session()
                       at the end of your program or don't pass it the end_state parameter
    """

    SUCCESS = "Success"
    FAIL = "Fail"
    INDETERMINATE = "Indeterminate"  # Default


class SessionExporter(SpanExporter):
    """
    Manages publishing events for Session
    """

    def __init__(self, session: Session, **kwargs):
        self.session = session
        self._shutdown = threading.Event()
        self._export_lock = threading.Lock()
        super().__init__(**kwargs)
        self.subagent_id = ""

    @property
    def endpoint(self):
        return f"{self.session.config.endpoint}/v2/create_events"
    

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:


        if self._shutdown.is_set():
            return SpanExportResult.SUCCESS

        with self._export_lock:
            try:
                # Skip if no spans to export
                if not spans:
                    return SpanExportResult.SUCCESS

                events = []
                for span in spans:
                    event_data = json.loads(span.attributes.get("event.data", "{}"))

                    # Extract agent_id consistently
                    agent_id = event_data.get("agent_id") or event_data.get("id")
                    
                    # Add debug logging
                    logger.debug(f"Processing span with agent_id: {agent_id}")
                    logger.debug(f"Current subagent_id: {self.subagent_id}")
                    
                    if agent_id and (
                        agent_id != self.subagent_id or 
                        not self._last_agent_transition or 
                        (datetime.now(timezone.utc) - self._last_agent_transition).total_seconds() > 1
                    ):
                        logger.info(f"Agent transition detected: {self.subagent_id} -> {agent_id}")
                        init_timestamp = span.attributes.get("event.timestamp")
                        end_timestamp = span.attributes.get("event.end_timestamp")
                        self._handle_agent_transition(agent_id,init_timestamp,end_timestamp)
                        self._last_agent_transition = datetime.now(timezone.utc)

                    # Format event data based on event type
                    if span.name == "actions":
                        formatted_data = {
                            "action_type": event_data.get("action_type", event_data.get("name", "unknown_action")),
                            # "params": event_data.get("params", {}),
                            # "returns": event_data.get("returns"),
                        }
                    elif span.name == "tools":
                        formatted_data = {
                            "name": event_data.get("name", event_data.get("tool_name", "unknown_tool")),
                            # "params": event_data.get("params", {}),
                            # "returns": event_data.get("returns"),
                        }
                    elif span.name == "llms":
                        event_data = {key: value for key, value in event_data.items() if key not in ["params", "returns"]}
                        completion_tokens = event_data.get("completion_tokens")
                        prompt_tokens = event_data.get("prompt_tokens")
                        model = event_data.get("model")
                        prompt_tokens_cost_usd_dollar, completion_tokens_cost_usd_dollar = cost_per_token(model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
                        total_cost_usd_dollar = prompt_tokens_cost_usd_dollar + completion_tokens_cost_usd_dollar
                        event_data["cost"] = total_cost_usd_dollar
                        self.session.token_cost = self.session.token_cost + Decimal(total_cost_usd_dollar)
                        formatted_data = event_data

                    else:
                        event_data = {key: value for key, value in event_data.items() if key not in ["params", "returns"]}
                        formatted_data = event_data

                    formatted_data = {**event_data, **formatted_data}
                    # Get timestamps, providing defaults if missing
                    current_time = datetime.now(timezone.utc).isoformat()
                    init_timestamp = span.attributes.get("event.timestamp")
                    end_timestamp = span.attributes.get("event.end_timestamp")

                    # Handle missing timestamps
                    if init_timestamp is None:
                        init_timestamp = current_time
                    if end_timestamp is None:
                        end_timestamp = current_time

                    # Get event ID, generate new one if missing
                    event_id = span.attributes.get("event.id")
                    if event_id is None:
                        event_id = str(uuid4())

                    agent_uuid, user_id, version= DatabaseManager.get_session_metadata(os.getenv("SESSION_ID"))

                    events.append(
                        {
                            "user_id": user_id,
                            "agent_uuid": agent_uuid,
                            "id": event_id,
                            "event_type": span.name,
                            "init_timestamp": init_timestamp,
                            "end_timestamp": end_timestamp,
                            **formatted_data,
                            "session_id": str(self.session.session_id),
                        }
                    )

                # Only make HTTP request if we have events and not shutdown
                if events:
                    try:
                        jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)
                        res = HttpClient.post(
                            self.endpoint,
                            json.dumps({"events": events}).encode("utf-8"),
                            api_key=self.session.config.api_key,
                            jwt=jwt,
                        )
                        return SpanExportResult.SUCCESS if res.code == 200 else SpanExportResult.FAILURE
                    except Exception as e:
                        logger.error(f"Failed to send events: {e}")
                        return SpanExportResult.FAILURE

                return SpanExportResult.SUCCESS
            


            except Exception as e:
                logger.error(f"Failed to export spans: {e}")
                return SpanExportResult.FAILURE
        
    def _handle_agent_transition(self, new_agent_id: str,  init_timestamp, end_timestamp) -> None:
        """Handle transition between agents only when the agent ID changes."""
        try:
            if self.subagent_id == new_agent_id:
                logger.info(f"Agent {new_agent_id} is already active. No status update needed.")
                return  # Skip status update if agent ID has not changed

            # Complete previous agent if it exists
            if self.subagent_id:
                logger.info(f"Completing previous agent: {self.subagent_id}")
                success = self.session.update_agent_status(self.subagent_id, init_timestamp, end_timestamp, AgentStatus.COMPLETED)
                if not success:
                    logger.error(f"Failed to update status for agent {self.subagent_id} to COMPLETED")
            
            # Update the new agent's status only if necessary
            current_status = self.session.status_tracker.get_agent_status(new_agent_id)
            logger.info(f"New agent {new_agent_id} current status: {current_status}")

            if current_status == AgentStatus.NOT_USED:
                success = self.session.update_agent_status(new_agent_id, init_timestamp, end_timestamp, AgentStatus.IN_PROGRESS)
                if not success:
                    logger.error(f"Failed to update status for agent {new_agent_id} to IN_PROGRESS")

            # Update the tracking variable
            self.subagent_id = new_agent_id
            logger.info(f"Updated subagent_id to: {self.subagent_id}")

        except Exception as e:
            logger.error(f"Error in agent transition: {e}")



class Session:
    """
    Represents a session of events, with a start and end state.

    Args:
        session_id (UUID): The session id is used to record particular runs.
        config (Configuration): The configuration object for the session.
        tags (List[str], optional): Tags that can be used for grouping or sorting later. Examples could be ["GPT-4"].
        host_env (dict, optional): A dictionary containing host and environment data.

    Attributes:
        init_timestamp (str): The ISO timestamp for when the session started.
        end_timestamp (str, optional): The ISO timestamp for when the session ended. Only set after end_session is called.
        end_state (str, optional): The final state of the session. Options: "Success", "Fail", "Indeterminate". Defaults to "Indeterminate".
        end_state_reason (str, optional): The reason for ending the session.
        session_id (UUID): Unique identifier for the session.
        tags (List[str]): List of tags associated with the session for grouping and filtering.
        video (str, optional): URL to a video recording of the session.
        host_env (dict, optional): Dictionary containing host and environment data.
        config (Configuration): Configuration object containing settings for the session.
        jwt (str, optional): JSON Web Token for authentication with the AgentOps API.
        token_cost (Decimal): Running total of token costs for the session.
        event_counts (dict): Counter for different types of events:
            - llms: Number of LLM calls
            - tools: Number of tool calls
            - actions: Number of actions
            - errors: Number of errors
            - apis: Number of API calls
        session_url (str, optional): URL to view the session in the AgentOps dashboard.
        is_running (bool): Flag indicating if the session is currently active.
    """

    def __init__(
        self,
        session_id: UUID,
        config: Configuration,
        tags: Optional[List[str]] = None,
        host_env: Optional[dict] = None,
    ):
        self.end_timestamp = None
        self.end_state: Optional[str] = "Indeterminate"
        self.session_id = session_id
        self.init_timestamp = get_ISO_time()
        self.tags: List[str] = tags or []
        self.video: Optional[str] = None
        self.end_state_reason: Optional[str] = None
        self.host_env = host_env
        self.config = config
        self.jwt = None
        self._lock = threading.Lock()
        self._end_session_lock = threading.Lock()
        self.token_cost: Decimal = Decimal(0)
        self._session_url: str = ""
        self.event_counts = {
            "llms": 0,
            "tools": 0,
            "actions": 0,
            "errors": 0,
            "apis": 0,
        }
        # self.session_url: Optional[str] = None

        # Start session first to get JWT
        self.is_running = self._start_session()
        if not self.is_running:
            return
        
        # After successful start, update status to IN_PROGRESS
        self.status = SessionStatus.IN_PROGRESS
        self._send_session_status_update(self.status.value)

        # Initialize OTEL components with a more controlled processor
        self._tracer_provider = TracerProvider()
        self._otel_tracer = self._tracer_provider.get_tracer(
            f"agentops.session.{str(session_id)}",
        )
        self._otel_exporter = SessionExporter(session=self)

        # Use smaller batch size and shorter delay to reduce buffering
        self._span_processor = BatchSpanProcessor(
            self._otel_exporter,
            max_queue_size=self.config.max_queue_size,
            schedule_delay_millis=self.config.max_wait_time,
            max_export_batch_size=min(
                max(self.config.max_queue_size // 20, 1),
                min(self.config.max_queue_size, 32),
            ),
            export_timeout_millis=20000,
        )

        self._tracer_provider.add_span_processor(self._span_processor)

        self.status_tracker = AgentStatusTracker()

        

    def set_video(self, video: str) -> None:
        """
        Sets a url to the video recording of the session.

        Args:
            video (str): The url of the video recording
        """
        self.video = video

    def _flush_spans(self) -> bool:
        """
        Flush pending spans for this specific session with timeout.
        Returns True if flush was successful, False otherwise.
        """
        if not hasattr(self, "_span_processor"):
            return True

        try:
            success = self._span_processor.force_flush(timeout_millis=self.config.max_wait_time)
            if not success:
                logger.warning("Failed to flush all spans before session end")
            return success
        except Exception as e:
            logger.warning(f"Error flushing spans: {e}")
            return False

    def end_session(
        self,
        end_state: str = "Indeterminate",
        end_state_reason: Optional[str] = None,
        video: Optional[str] = None,
    ) -> Union[Decimal, None]:
        with self._end_session_lock:
            if not self.is_running:
                return None

            if not any(end_state == state.value for state in EndState):
                logger.warning("Invalid end_state. Please use one of the EndState")
                return None

            try:
                # Update session status based on end_state
                if end_state == EndState.SUCCESS.value:
                    self.status = SessionStatus.COMPLETED
                elif end_state == EndState.FAIL.value:
                    self.status = SessionStatus.FAILED
                else:
                    # For INDETERMINATE, default to COMPLETED
                    self.status = SessionStatus.COMPLETED

                # Update current agent's status based on session end state
                current_agent_id = self._otel_exporter.subagent_id
                if current_agent_id:
                    current_timestamp = get_ISO_time()
                    if end_state == EndState.SUCCESS.value:
                        self.update_agent_status(current_agent_id, current_timestamp, current_timestamp, AgentStatus.COMPLETED)
                    elif end_state == EndState.FAIL.value:
                        self.update_agent_status(current_agent_id, current_timestamp, current_timestamp, AgentStatus.FAILED)
                    # For INDETERMINATE, we'll mark it as completed as per session behavior
                    else:
                        self.update_agent_status(current_agent_id, current_timestamp, current_timestamp, AgentStatus.COMPLETED)
                
                # Send status update before other cleanup
                self._send_session_status_update(self.status.value)


                # Force flush any pending spans before ending session
                if hasattr(self, "_span_processor"):
                    self._span_processor.force_flush(timeout_millis=5000)

                # 1. Set shutdown flag on exporter first
                if hasattr(self, "_otel_exporter"):
                    self._otel_exporter.shutdown()

                # 2. Set session end state
                self.end_timestamp = get_ISO_time()
                self.end_state = end_state
                self.end_state_reason = end_state_reason
                if video is not None:
                    self.video = video

                # 3. Mark session as not running before cleanup
                self.is_running = False

                # 4. Clean up OTEL components
                if hasattr(self, "_span_processor"):
                    try:
                        # Force flush any pending spans
                        self._span_processor.force_flush(timeout_millis=5000)
                        # Shutdown the processor
                        self._span_processor.shutdown()
                    except Exception as e:
                        logger.warning(f"Error during span processor cleanup: {e}")
                    finally:
                        del self._span_processor

                # 5. Final session update
                if not (analytics_stats := self.get_analytics()):
                    return None
                
                self._send_summary()

                analytics = (
                    f"Session Stats - "
                    f"{colored('Duration:', attrs=['bold'])} {analytics_stats['Duration']} | "
                    f"{colored('Cost:', attrs=['bold'])} ${analytics_stats['Cost']} | "
                    f"{colored('LLMs:', attrs=['bold'])} {analytics_stats['LLM calls']} | "
                    f"{colored('Tools:', attrs=['bold'])} {analytics_stats['Tool calls']} | "
                    f"{colored('Actions:', attrs=['bold'])} {analytics_stats['Actions']} | "
                    f"{colored('Errors:', attrs=['bold'])} {analytics_stats['Errors']}"
                )
                logger.info(analytics)

            except Exception as e:
                # If there's an error during end_session, mark as FAILED
                self.status = SessionStatus.FAILED
                
                # Also mark current agent as failed
                current_agent_id = self._otel_exporter.subagent_id
                if current_agent_id:
                    current_timestamp = get_ISO_time()
                    self.update_agent_status(current_agent_id, current_timestamp, current_timestamp, AgentStatus.FAILED)

                self._send_session_status_update(self.status.value)

                logger.exception(f"Error during session end: {e}")
            finally:
                active_sessions.remove(self)  # First thing, get rid of the session

                logger.info(
                    colored(
                        f"\x1b[34mSession Replay: {self.session_url}\x1b[0m",
                        "blue",
                    )
                )
            return self.token_cost

    def add_tags(self, tags: List[str]) -> None:
        """
        Append to session tags at runtime.
        """
        if not self.is_running:
            return

        if not (isinstance(tags, list) and all(isinstance(item, str) for item in tags)):
            if isinstance(tags, str):
                tags = [tags]

        # Initialize tags if None
        if self.tags is None:
            self.tags = []

        # Add new tags that don't exist
        for tag in tags:
            if tag not in self.tags:
                self.tags.append(tag)

        # Update session state immediately
        self._update_session()

    def set_tags(self, tags):
        """Set session tags, replacing any existing tags"""
        if not self.is_running:
            return

        if not (isinstance(tags, list) and all(isinstance(item, str) for item in tags)):
            if isinstance(tags, str):
                tags = [tags]

        # Set tags directly
        self.tags = tags.copy()  # Make a copy to avoid reference issues

        # Update session state immediately
        self._update_session()

    def record(self, event: Union[Event, ErrorEvent], flush_now=False):
        """Record an event using OpenTelemetry spans"""
        if not self.is_running:
            return

        # Ensure event has all required base attributes
        if not hasattr(event, "id"):
            event.id = uuid4()
        if not hasattr(event, "init_timestamp"):
            event.init_timestamp = get_ISO_time()
        if not hasattr(event, "end_timestamp") or event.end_timestamp is None:
            event.end_timestamp = get_ISO_time()

        # Create session context
        token = set_value("session.id", str(self.session_id))

        try:
            token = attach(token)

            # Create a copy of event data to modify
            event_data = dict(filter_unjsonable(event.__dict__))

            # Add required fields based on event type
            if isinstance(event, ErrorEvent):
                event_data["error_type"] = getattr(event, "error_type", event.event_type)
            elif event.event_type == "actions":
                # Ensure action events have action_type
                if "action_type" not in event_data:
                    event_data["action_type"] = event_data.get("name", "unknown_action")
                if "name" not in event_data:
                    event_data["name"] = event_data.get("action_type", "unknown_action")
            elif event.event_type == "tools":
                # Ensure tool events have name
                if "name" not in event_data:
                    event_data["name"] = event_data.get("tool_name", "unknown_tool")
                if "tool_name" not in event_data:
                    event_data["tool_name"] = event_data.get("name", "unknown_tool")

            with self._otel_tracer.start_as_current_span(
                name=event.event_type,
                attributes={
                    "event.id": str(event.id),
                    "event.type": event.event_type,
                    "event.timestamp": event.init_timestamp or get_ISO_time(),
                    "event.end_timestamp": event.end_timestamp or get_ISO_time(),
                    "session.id": str(self.session_id),
                    "session.tags": ",".join(self.tags) if self.tags else "",
                    "event.data": json.dumps(event_data),
                },
            ) as span:
                if event.event_type in self.event_counts:
                    self.event_counts[event.event_type] += 1

                if isinstance(event, ErrorEvent):
                    span.set_attribute("error", True)
                    if hasattr(event, "trigger_event") and event.trigger_event:
                        span.set_attribute("trigger_event.id", str(event.trigger_event.id))
                        span.set_attribute("trigger_event.type", event.trigger_event.event_type)

                if flush_now and hasattr(self, "_span_processor"):
                    self._span_processor.force_flush()
        finally:
            detach(token)

    def _send_event(self, event):
        """Direct event sending for testing"""
        try:
            payload = {
                "events": [
                    {
                        "id": str(event.id),
                        "event_type": event.event_type,
                        "init_timestamp": event.init_timestamp,
                        "end_timestamp": event.end_timestamp,
                        "data": filter_unjsonable(event.__dict__),
                    }
                ]
            }

            jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)

            HttpClient.post(
                f"{self.config.endpoint}/v2/create_events",
                json.dumps(payload).encode("utf-8"),
                jwt=jwt,
            )
        except Exception as e:
            logger.error(f"Failed to send event: {e}")

    def send_agent_status_update(self, agent_id: str, timestamp, status: AgentStatus) -> bool:
        """Send agent status update to server."""
        if not self.is_running:
            logger.warning("Attempted to update agent status when session is not running")
            return False

        agent_data = self.status_tracker.get_agent_data(agent_id)
        if not agent_data:
            logger.warning(f"Attempted to update status for non-existent agent: {agent_id}")
            return False

        try:
            agent_uuid, user_id, version = DatabaseManager.get_session_metadata(os.getenv("SESSION_ID"))
        except Exception as e:
            logger.error(f"Failed to get session metadata: {e}")
            return False

        payload = {
            "agent_id": agent_id,
            "name": agent_data["name"],
            "status": status.value,
            "session_id": str(self.session_id),
            "timestamp": timestamp,
            "user_id": user_id,
            "agent_uuid": agent_uuid
        }

        # print(payload)

        try:
            jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)
            response = HttpClient.post(
                f"{self.config.endpoint}/v2/subagent/status",
                safe_serialize(payload).encode("utf-8"),
                jwt=jwt,
                api_key=self.config.api_key
            )
            
            if response.code == 200:
                logger.info(f"Successfully updated status for agent {agent_id} to {status.value}")
                return True
            else:
                logger.error(f"Server returned error code {response.code}")
                return False
                
        except ApiServerException as e:
            logger.error(f"Could not update agent status - {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send agent status update: {e}")
            return False

    def update_agent_status(self, agent_id: str, init_timestamp, end_timestamp, status: AgentStatus) -> bool:
        """Update agent status locally and on server."""
        logger.info(f"Updating status for agent {agent_id} to {status.value}")
        
        # First update local status
        if not self.status_tracker.update_status(agent_id, status):
            logger.error(f"Failed to update agent {agent_id} status locally")
            return False
            
        if self.status_tracker.get_agent_status(agent_id) == "completed":
            timestamp = end_timestamp
        else:
            timestamp = init_timestamp

        # Then update server
        server_update_success = self.send_agent_status_update(agent_id, timestamp, status)
        
        if not server_update_success:
            # Rollback local status if server update fails
            logger.warning(f"Server update failed for agent {agent_id}, rolling back local status")
            previous_status = self.status_tracker.get_agent_status(agent_id)
            self.status_tracker.update_status(agent_id, previous_status)
            return False
            
        return True

    def _reauthorize_jwt(self) -> Union[str, None]:
        with self._lock:
            payload = {"session_id": self.session_id}
            serialized_payload = json.dumps(filter_unjsonable(payload)).encode("utf-8")
            res = HttpClient.post(
                f"https://api.agentops.ai/v2/reauthorize_jwt",
                serialized_payload,
                self.config.api_key,
            )

            logger.debug(res.body)

            if res.code != 200:
                return None

            jwt = res.body.get("jwt", None)
            self.jwt = jwt
            return jwt

    def _start_session(self):
        with self._lock:

            jwt = os.getenv("JWT_TOKEN")
            #print(jwt)

            if jwt is None:

                try:
                    token = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)
                    
                except Exception as e:
                    print(f"Error: {e}")

                jwt = token

                if jwt is None:
                    return False
            
            self.jwt = jwt

            logger.info(
                colored(
                    "\x1b[34mSession Replay: {self.session_url}\x1b[0m",
                    "blue",)
                    )

            print("session is created")

            return True

    def _update_session(self) -> None:
        """Update session state on the server"""
        if not self.is_running:
            return

        # TODO: Determine whether we really need to lock here: are incoming calls coming from other threads?
        with self._lock:
            payload = {"session": self.__dict__}

            try:
                jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)
                res = HttpClient.post(
                    f"{self.config.endpoint}/v2/update_session",
                    json.dumps(filter_unjsonable(payload)).encode("utf-8"),
                    # self.config.api_key,
                    jwt=jwt,
                )
            except ApiServerException as e:
                return logger.error(f"Could not update session - {e}")

    def create_agent(self, name, agent_id):
        if not self.is_running:
            return
        if agent_id is None:
            agent_id = str(uuid4())

        payload = {
            "id": agent_id,
            "name": name,
        }

        self.status_tracker.add_agent(agent_id,name)

        status = self.status_tracker.get_agent_status(agent_id)

        timestamp = datetime.now(timezone.utc).isoformat()

        self.send_agent_status_update(agent_id,timestamp, status)

        serialized_payload = safe_serialize(payload).encode("utf-8")
        try:
            jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)
            HttpClient.post(
                f"{self.config.endpoint}/v2/create_agent",
                serialized_payload,
                api_key=self.config.api_key,
                jwt=jwt,
            )
        except ApiServerException as e:
            return logger.error(f"Could not create agent - {e}")

        return agent_id

    def patch(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            kwargs["session"] = self
            return func(*args, **kwargs)

        return wrapper

    def _get_response(self) -> Optional[Response]:
        payload = {"session": self.__dict__}
        #print(json.dumps(filter_unjsonable(payload)))
        try:
            jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)
            response = HttpClient.post(
                f"{self.config.endpoint}/v2/update_session",
                json.dumps(filter_unjsonable(payload)).encode("utf-8"),
                api_key=self.config.api_key,
                jwt=jwt,
            )
        except ApiServerException as e:
            return logger.error(f"Could not end session - {e}")

        logger.debug(response.body)
        return response

    def _format_duration(self, start_time, end_time) -> str:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        duration = end - start

        hours, remainder = divmod(duration.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if hours > 0:
            parts.append(f"{int(hours)}h")
        if minutes > 0:
            parts.append(f"{int(minutes)}m")
        parts.append(f"{seconds:.1f}s")

        return " ".join(parts)

    def _get_token_cost(self, response: Response) -> Decimal:
        token_cost = self.token_cost
        if token_cost == "unknown" or token_cost is None:
            return Decimal(0)
        return Decimal(token_cost)

    def _format_token_cost(self, token_cost: Decimal) -> str:
        return (
            "{:.2f}".format(token_cost)
            if token_cost == 0
            else "{:.6f}".format(token_cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
        )

    def get_analytics(self) -> Optional[Dict[str, Any]]:
        if not self.end_timestamp:
            self.end_timestamp = get_ISO_time()

        formatted_duration = self._format_duration(self.init_timestamp, self.end_timestamp)

        # if (response := self._get_response()) is None:
        #     return None

        # self.token_cost = self._get_token_cost(response)
        agent_uuid, user_id, version = DatabaseManager.get_session_metadata(os.getenv("SESSION_ID"))
        return {
            "session_id": str(self.session_id),
            "user_id": user_id,
            "agent_uuid": agent_uuid,
            "version": version,
            "LLM calls": self.event_counts["llms"],
            "Tool calls": self.event_counts["tools"],
            "Actions": self.event_counts["actions"],
            "Errors": self.event_counts["errors"],
            "Duration": formatted_duration,
            "Cost": self._format_token_cost(self.token_cost),
        }
    
    def _send_summary(self):
        """Direct event sending for testing"""
        try:
            summary = self.get_analytics()
            payload = {
                "summary": [summary]
            }

            jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)

            HttpClient.post(
                f"{self.config.endpoint}/v2/create_summary",
                json.dumps(payload).encode("utf-8"),
                jwt=jwt,
            )
        except Exception as e:
            logger.error(f"Failed to send event: {e}")

    @property
    def session_url(self) -> str:
        """Returns the URL for this session in the AgentOps dashboard."""
        # assert self.e, "Session ID is required to generate a session URL"
        return f"{self.config.endpoint}/drilldown?session_id={self.session_id}"

    # @session_url.setter
    # def session_url(self, url: str):
    #     pass

    def _send_session_status_update(self, status: str) -> None:
        """Send session status update to server.
        
        Args:
            status (str): Current status of the session (created/in_progress/completed/failed)
        """
        try:
            agent_uuid, user_id, version = DatabaseManager.get_session_metadata(os.getenv("SESSION_ID"))
            
            payload = {
                "session_id": self.session_id,
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "agent_uuid": agent_uuid
            }

            jwt = generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url)

            response = HttpClient.post(
                f"{self.config.endpoint}/v2/session/status",
                safe_serialize(payload).encode("utf-8"),
                jwt=jwt,
                api_key=self.config.api_key
            )
            
            if response.code == 200:
                logger.info(f"Session {self.session_id} status updated to {status}")
            else:
                logger.error(f"Failed to update session status: {response.code}")

        except Exception as e:
            logger.error(f"Failed to send session status update: {e}")



active_sessions: List[Session] = []