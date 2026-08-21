import os

from dotenv import load_dotenv

load_dotenv()

env = os.getenv

DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = env("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_BASE_URL = env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

NEO4J_URI = env("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = env("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = env("NEO4J_PASSWORD")
NEO4J_DATABASE = env("NEO4J_DATABASE", "neo4j")
