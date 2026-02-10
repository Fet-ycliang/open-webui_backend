import os
from sqlalchemy import create_engine, Engine
from dotenv import load_dotenv

# Load environment variables from the .env file in the service's root directory
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine: Engine | None = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL)
        print("Database engine created successfully.")
    except Exception as e:
        print(f"Failed to create database engine: {e}")
else:
    print("WARNING: DATABASE_URL environment variable not set.")

def get_engine() -> Engine | None:
    """Returns the shared SQLAlchemy engine instance."""
    return engine
