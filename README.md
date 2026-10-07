# MAIGO Web Deployment

## What this is
A deployable single-service version of the current MAIGO V20 semantic-search build. The backend serves the frontend at the public root URL, while local Windows development can still use the separate frontend server.

## Deploy
1. Put this folder into a GitHub repository.
2. In Render, create a Web Service from the repository. Render supports FastAPI web services and free web services for testing.
3. Use the included `render.yaml`, or set:
   - Build: `apt-get update && apt-get install -y ffmpeg && pip install -r requirements.txt`
   - Start: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
4. Open the generated `onrender.com` URL.

## Free-tier limitation
The free Render web service is not a GPU machine and has an ephemeral filesystem. It is suitable for a public prototype/test, not heavy production AI/video processing. The local Qwen/Ollama brain should remain on your PC until a hosted AI/GPU service is connected.

## Local
Backend: `cd backend` then run your existing Windows uvicorn command. Frontend: `cd frontend` then `py -m http.server 5500`.
