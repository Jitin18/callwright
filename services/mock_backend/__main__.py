import uvicorn
uvicorn.run("services.mock_backend.app:app", host="0.0.0.0", port=8100)
