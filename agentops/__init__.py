# agentops/__init__.py
import sys
from typing import Optional, List, Union

from .client import Client
from .event import Event, ActionEvent, LLMEvent, ToolEvent, ErrorEvent
from .decorators import record_action, track_agent, record_tool, record_function
from .helpers import check_agentops_update
from .log_config import logger
from .session import Session
import threading
from importlib.metadata import version as get_version
from packaging import version
from .llms import tracker

try:
    from .partners.langchain_callback_handler import (
        LangchainCallbackHandler,
        AsyncLangchainCallbackHandler,
    )
except ModuleNotFoundError:
    pass

if "autogen" in sys.modules:
    Client().configure(instrument_llm_calls=False)
    Client()._initialize_autogen_logger()
    Client().add_default_tags(["autogen"])

if "crewai" in sys.modules:
    crew_version = version.parse(get_version("crewai"))

    # uses langchain, greater versions will use litellm and default is to instrument
    if crew_version < version.parse("0.56.0"):
        Client().configure(instrument_llm_calls=False)

    Client().add_default_tags(["crewai"])


import threading
from typing import Optional, List, Union
from agentops import Client, Session
import logging

logger = logging.getLogger(__name__)

def init(
    api_key: Optional[str] = "1234",  # Allow bypassing by setting to None
    parent_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    max_wait_time: Optional[int] = None,
    max_queue_size: Optional[int] = None,
    tags: Optional[List[str]] = None,  # Deprecated
    default_tags: Optional[List[str]] = None,
    instrument_llm_calls: Optional[bool] = None,
    auto_start_session: Optional[bool] = None,
    inherited_session_id: Optional[str] = None,
    skip_auto_end_session: Optional[bool] = None,
) -> Union[Session, None]:
    """
    Initializes the AgentOps singleton, allowing optional API key usage.

    Args:
        api_key (str, optional): API Key for AgentOps services. Can be None to bypass.
        parent_key (str, optional): Organization key.
        endpoint (str, optional): API endpoint.
        max_wait_time (int, optional): Max wait time in ms.
        max_queue_size (int, optional): Max size of event queue.
        tags (List[str], optional): [Deprecated] Use `default_tags` instead.
        default_tags (List[str], optional): Default tags for session grouping.
        instrument_llm_calls (bool, optional): Enable LLM call instrumentation.
        auto_start_session (bool, optional): Auto-start session upon initialization.
        inherited_session_id (str, optional): Use an existing session ID.
        skip_auto_end_session (bool, optional): Prevents auto-ending session.

    Returns:
        Session object if a session is started, else None.
    """

    client = Client()
    client.unsuppress_logs()

    # Start background thread for updates
    t = threading.Thread(target=check_agentops_update, daemon=True)
    t.start()

    # Prevent re-initialization
    if client.is_initialized:
        logger.warning("AgentOps is already initialized. Use agentops.start_session() instead.")
        return None

    # Handle deprecated 'tags' parameter
    if tags:
        logger.warning("The 'tags' parameter is deprecated. Use 'default_tags' instead.")
        if default_tags is None:
            default_tags = tags

    # Configure AgentOps client WITHOUT requiring an API key
    client_config = {
        "parent_key": parent_key,
        "endpoint": endpoint,
        "max_wait_time": max_wait_time,
        "max_queue_size": max_queue_size,
        "default_tags": default_tags,
        "instrument_llm_calls": instrument_llm_calls,
        "auto_start_session": auto_start_session,
        "skip_auto_end_session": skip_auto_end_session,
    }

    # Only include API key if it's provided
    if api_key:
        client_config["api_key"] = "1234"

    client.configure(**client_config)

    # Handle inherited session logic
    if inherited_session_id and inherited_session_id.strip():
        if auto_start_session is False:
            client.add_pre_init_warning(
                "auto_start_session is False - inherited_session_id will not be used."
            )
            return client.initialize()

        client.configure(auto_start_session=False)
        client.initialize()
        return client.start_session(inherited_session_id=inherited_session_id)

    # Default initialization
    return client.initialize()



def configure(
    api_key: Optional[str] = "1234",
    parent_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    max_wait_time: Optional[int] = None,
    max_queue_size: Optional[int] = None,
    default_tags: Optional[List[str]] = None,
    instrument_llm_calls: Optional[bool] = None,
    auto_start_session: Optional[bool] = None,
    skip_auto_end_session: Optional[bool] = None,
):
    """
    Configure the AgentOps Client

    Args:
        api_key (str, optional): API Key for AgentOps services.
        parent_key (str, optional): Organization key to give visibility of all user sessions the user's organization.
        endpoint (str, optional): The endpoint for the AgentOps service.
        max_wait_time (int, optional): The maximum time to wait in milliseconds before flushing the queue.
        max_queue_size (int, optional): The maximum size of the event queue
        default_tags (List[str], optional): Default tags for the sessions that can be used for grouping or sorting later (e.g. ["GPT-4"]).
        instrument_llm_calls (bool, optional): Whether to instrument LLM calls and emit LLMEvents.
        auto_start_session (bool, optional): Whether to start a session automatically when the client is created.
        skip_auto_end_session (bool, optional): Don't automatically end session based on your framework's decision-making
            (i.e. Crew determining when tasks are complete and ending the session)
    """
    Client().configure(
        api_key="1234",
        parent_key=parent_key,
        endpoint=endpoint,
        max_wait_time=max_wait_time,
        max_queue_size=max_queue_size,
        default_tags=default_tags,
        instrument_llm_calls=instrument_llm_calls,
        auto_start_session=auto_start_session,
        skip_auto_end_session=skip_auto_end_session,
    )


def start_session(
    tags: Optional[List[str]] = None,
    inherited_session_id: Optional[str] = None,
) -> Union[Session, None]:
    """
    Start a new session for recording events.

    Args:
        tags (List[str], optional): Tags that can be used for grouping or sorting later.
            e.g. ["test_run"].
        inherited_session_id: (str, optional): Set the session ID to inherit from another client
    """
    Client().unsuppress_logs()

    if not Client().is_initialized:
        return logger.warning(
            "AgentOps has not been initialized yet. Please call agentops.init() before starting a session"
        )

    return Client().start_session(tags, inherited_session_id)


def end_session(
    end_state: str,
    end_state_reason: Optional[str] = None,
    video: Optional[str] = None,
    is_auto_end: Optional[bool] = False,
):
    """
    End the current session with the AgentOps service.

    Args:
        end_state (str): The final state of the session. Options: Success, Fail, or Indeterminate.
        end_state_reason (str, optional): The reason for ending the session.
        video (str, optional): URL to a video recording of the session
    """
    Client().unsuppress_logs()

    if Client().is_multi_session:
        return logger.warning(
            "Could not end session - multiple sessions detected. You must use session.end_session() instead of agentops.end_session()"
            + " More info: https://docs.agentops.ai/v1/concepts/core-concepts#session-management"
        )

    if not Client().has_sessions:
        return logger.warning("Could not end session - no sessions detected")

    Client().end_session(
        end_state=end_state,
        end_state_reason=end_state_reason,
        video=video,
        is_auto_end=is_auto_end,
    )


def record(event: Union[Event, ErrorEvent]):
    """
    Record an event with the AgentOps service.

    Args:
        event (Event): The event to record.
    """
    Client().unsuppress_logs()

    if Client().is_multi_session:
        return logger.warning(
            "Could not record event - multiple sessions detected. You must use session.record() instead of agentops.record()"
            + " More info: https://docs.agentops.ai/v1/concepts/core-concepts#session-management"
        )

    if not Client().has_sessions:
        return logger.warning(
            "Could not record event - no sessions detected. Create a session by calling agentops.start_session()"
        )

    Client().record(event)


def add_tags(tags: List[str]):
    """
    Append to session tags at runtime.

    Args:
        tags (List[str]): The list of tags to append.
    """
    if Client().is_multi_session:
        return logger.warning(
            "Could not add tags to session - multiple sessions detected. You must use session.add_tags() instead of agentops.add_tags()"
            + " More info: https://docs.agentops.ai/v1/concepts/core-concepts#session-management"
        )

    if not Client().has_sessions:
        return logger.warning(
            "Could not add tags to session - no sessions detected. Create a session by calling agentops.start_session()"
        )

    Client().add_tags(tags)


def set_tags(tags: List[str]):
    """
    Replace session tags at runtime.

    Args:
        tags (List[str]): The list of tags to set.
    """
    if Client().is_multi_session:
        return logger.warning(
            "Could not set tags on session - multiple sessions detected. You must use session.set_tags() instead of agentops.set_tags()"
            + " More info: https://docs.agentops.ai/v1/concepts/core-concepts#session-management"
        )

    if not Client().has_sessions:
        return logger.warning(
            "Could not set tags on session - no sessions detected. Create a session by calling agentops.start_session()"
        )

    Client().set_tags(tags)


def get_api_key() -> Union[str, None]:
    return Client().api_key


def set_api_key(api_key: str) -> None:
    Client().configure(api_key="1234")


def set_parent_key(parent_key: str):
    """
    Set the parent API key so another organization can view data.

    Args:
        parent_key (str): The API key of the parent organization to set.
    """
    Client().configure(parent_key=parent_key)


def stop_instrumenting():
    if Client().is_initialized:
        Client().stop_instrumenting()


def create_agent(name: str, agent_id: Optional[str] = None):
    if Client().is_multi_session:
        return logger.warning(
            "Could not create agent - multiple sessions detected. You must use session.create_agent() instead of agentops.create_agent()"
            + " More info: https://docs.agentops.ai/v1/concepts/core-concepts#session-management"
        )

    if not Client().has_sessions:
        return logger.warning(
            "Could not create agent - no sessions detected. Create a session by calling agentops.start_session()"
        )

    return Client().create_agent(name=name, agent_id=agent_id)


def get_session(session_id: str):
    """
    Get an active (not ended) session from the AgentOps service

    Args:
        session_id (str): the session id for the session to be retreived
    """
    Client().unsuppress_logs()

    return Client().get_session(session_id)


# Mostly used for unit testing -
# prevents unexpected sessions on new tests
def end_all_sessions() -> None:
    return Client().end_all_sessions()
