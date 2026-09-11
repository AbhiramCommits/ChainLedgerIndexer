from fastapi import FastAPI

from chainledger.api.routes import router

app = FastAPI(title="chainledger", version="0.2.0")
app.include_router(router)
