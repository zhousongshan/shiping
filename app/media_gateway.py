"""Only signed, expiring model-reference blobs; never exposes the application UI."""
import hashlib
import hmac
import re
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from . import config

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

def signature(name, expires):
    return hmac.new(config.media_secret_path().read_bytes(),f"{name}:{expires}".encode(),hashlib.sha256).hexdigest()

@app.api_route("/media/{name}",methods=["GET","HEAD"])
def reference(name:str,expires:int,signature_value:str):
    if not re.fullmatch(r"[a-f0-9]{64}\.(mp4|wav|mp3)",name):
        raise HTTPException(404)
    if expires<int(time.time()) or not hmac.compare_digest(signature(name,expires),signature_value):
        raise HTTPException(403,"Reference expired or signature invalid")
    path=config.media_storage()/"reference-blobs"/name
    if not path.is_file():raise HTTPException(404)
    mime="video/mp4" if name.endswith("mp4") else "audio/wav" if name.endswith("wav") else "audio/mpeg"
    return FileResponse(path,media_type=mime,headers={"Cache-Control":"private, max-age=60"})
