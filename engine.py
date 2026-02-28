# Re-export for Render deployment (uvicorn engine:app)
# The actual code lives in backend/engine.py
from backend.engine import app  # noqa: F401
