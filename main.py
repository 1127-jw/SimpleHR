# from fastapi import FastAPI
#
# app = FastAPI()
#
#
# @app.get("/")
# async def root():
#     return {"message": "Hello World"}
#
#
# @app.get("/hello/{name}")
# async def say_hello(name: str):
#     return {"message": f"Hello {name}"}

"""兼容入口，等价于 ``app.main:app``。

推荐直接使用::

    uvicorn app.main:app --reload

保留本文件是为了兼容 ``python main.py`` 启动方式。只负责本地开发启动
"""

from app.main import app
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)