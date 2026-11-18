"""Arranca el servidor: .venv/Scripts/python.exe -m app"""

import os

import uvicorn

if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    uvicorn.run("app.main:crear_app", factory=True,
                host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
