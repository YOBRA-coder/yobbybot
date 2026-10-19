"""
Entrypoint. Run with:  python run.py
(equivalent to: uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload)
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)
