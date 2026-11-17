import os
from dotenv import load_dotenv
import uvicorn

load_dotenv()
if __name__ == "__main__":
    uvicorn.run("app.api:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8790")), proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", access_log=False)
