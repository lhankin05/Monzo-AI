import os
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()
app = FastAPI(title="Monzo-AI", version="0.1.0")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/config-status")
def config_status():
    keys = ("MONZO_CLIENT_ID", "MONZO_CLIENT_SECRET", "MONZO_REDIRECT_URI")
    return {"configured": {key: bool(os.getenv(key)) for key in keys}}
