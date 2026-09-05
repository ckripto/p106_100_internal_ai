"""Flask application factory and production entry point."""

import logging
import signal
import threading

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .settings import SETTINGS
from .store import APIError, Store
from .worker import Worker


def create_app(database_path=None):
    app = Flask(
        __name__,
        static_folder=str(SETTINGS.static_path),
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = SETTINGS.max_request_body_size
    app.json.ensure_ascii = False
    store = Store(database_path or SETTINGS.database_path)
    app.extensions["store"] = store

    @app.before_request
    def validate_mutation():
        if request.method in {"POST", "DELETE"}:
            if request.headers.get("Origin") not in {None, request.host_url.rstrip("/")}:
                raise APIError("Запрос должен быть отправлен с этой страницы", 403)
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise APIError("Запрос должен быть отправлен с этой страницы", 403)
            if not request.is_json:
                raise APIError("Ожидается JSON", 415)
            if not isinstance(request.get_json(), dict):
                raise APIError("Ожидается объект JSON")

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="Некорректный запрос" if error.code == 400 else error.name), error.code

    @app.get("/")
    def index():
        return send_from_directory(SETTINGS.static_path, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/sessions")
    def sessions():
        return jsonify(sessions=store.sessions())

    @app.post("/api/sessions")
    def create_session():
        return jsonify(store.create_session()), 201

    @app.get("/api/sessions/<session_id>")
    def session_detail(session_id):
        return jsonify(store.session_detail(session_id, request.args.get("before", type=int)))

    @app.delete("/api/sessions/<session_id>")
    def delete_session(session_id):
        store.delete_session(session_id)
        return "", 204

    @app.post("/api/sessions/<session_id>/tasks")
    def submit(session_id):
        data = request.get_json()
        task = store.submit(session_id, data.get("prompt"), data.get("request_id"))
        if "worker" in app.extensions:
            app.extensions["worker"].wake.set()
        return jsonify(task), 202

    return app


def main():
    import fcntl
    from waitress import serve

    logging.basicConfig(level=logging.INFO)
    app = create_app()
    store = app.extensions["store"]
    with open(str(store.path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover_interrupted()
        worker = Worker(store)
        app.extensions["worker"] = worker
        threading.Thread(target=worker.run, name="coordinator-worker", daemon=True).start()

        def shutdown(*_):
            worker.stop.set()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        serve(
            app,
            host=SETTINGS.host,
            port=SETTINGS.port,
            threads=SETTINGS.http_threads,
            max_request_body_size=SETTINGS.max_request_body_size,
            channel_timeout=SETTINGS.channel_timeout,
        )


if __name__ == "__main__":
    main()
