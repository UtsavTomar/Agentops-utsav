import os
import psycopg2
from psycopg2 import sql


class DatabaseManager():
    def __init__(self, db_name):
        cursor = None
        connection = None

    def get_session_metadata(session_id):
    
        try:
            connection = psycopg2.connect(os.getenv("DB_CONNECTION_STRING"))
            cursor = connection.cursor()
            
            query = sql.SQL("""
                SELECT agent_uuid, user_id, version
                FROM "agentic-platform".session_id_metadata
                WHERE session_id = %s;
            """)
            
            cursor.execute(query, (session_id,))
            
            result = cursor.fetchone()
            
            if result:
                agent_uuid = result[0]
                user_id = result[1]
                version = result[2]
                return agent_uuid, user_id, version
            else:
                return None
                
        except Exception as e:
            print(f"An error occurred: {e}")
            return None
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()