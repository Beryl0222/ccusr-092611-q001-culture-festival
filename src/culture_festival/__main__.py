"""python3 -m culture_festival 启动编排台服务。"""
import os

from .api import serve

if __name__ == "__main__":
    serve(host=os.environ.get("HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "8080")),
          database=os.environ.get("FESTIVAL_DB", "festival.db"))
