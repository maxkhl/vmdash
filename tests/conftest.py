import time

import pytest

from app.config import Config
from app.web import create_app


def make_config(**overrides):
    values = dict(
        backend="mock",
        mock_delay=0,
        prompt_timeout=5,
        unlock_timeout=5,
        retry_timeout=5,
        agent_timeout=5,
    )
    values.update(overrides)
    cfg = Config(**values)
    cfg.validate()
    return cfg


@pytest.fixture
def make_app():
    def factory(**overrides):
        cfg = make_config(**overrides)
        app = create_app(cfg)
        app.config["TESTING"] = True
        return app

    return factory


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def backend(app):
    return app.extensions["vmdash"]["backend"]


def wait_job(client, job_id, states=("unlocked", "done", "failed"), timeout=10, awaiting=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["state"] in states or (awaiting and job["awaiting_passphrase"]):
            return job
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} hängt im Zustand {job['state']}")
