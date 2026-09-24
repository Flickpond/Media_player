import argparse
import os
import sys
from http.cookies import SimpleCookie
from uuid import UUID

from locust import HttpUser, constant_pacing, task
from locust.exception import StopUser

LOCAL_URL = "http://127.0.0.1:13000"


def check_local_target():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-H", "--host")
    options, _other_args = parser.parse_known_args(sys.argv[1:])
    if options.host != LOCAL_URL:
        raise SystemExit(f"This scenario only targets {LOCAL_URL}")
    UUID(os.environ["LOAD_JOB_ID"])
    if not os.environ.get("LOAD_EMAIL") or not os.environ.get("LOAD_PASSWORD"):
        raise SystemExit("LOAD_EMAIL and LOAD_PASSWORD are required")


check_local_target()


class JobReader(HttpUser):
    wait_time = constant_pacing(2)

    def on_start(self):
        if self.client.base_url.rstrip("/") != LOCAL_URL:
            raise StopUser()
        with self.client.post(
            "/api/auth/login",
            json={"email": os.environ["LOAD_EMAIL"], "password": os.environ["LOAD_PASSWORD"]},
            name="SETUP login",
            catch_response=True,
        ) as response:
            cookies = SimpleCookie()
            cookies.load(response.headers.get("Set-Cookie", ""))
            if response.status_code != 200 or "access_token" not in cookies:
                response.failure("login failed or access_token cookie missing")
                raise StopUser()
            self.client.headers["Cookie"] = f"access_token={cookies['access_token'].value}"
        self.job_id = os.environ["LOAD_JOB_ID"]
        self.iteration = 0

    @task
    def watch_job(self):
        self.iteration += 1
        with self.client.get(
            f"/api/jobs/{self.job_id}", name="GET job status", catch_response=True
        ) as response:
            try:
                job = response.json()
                if (
                    response.status_code != 200
                    or job["id"] != self.job_id
                    or job["status"] != "done"
                ):
                    response.failure("unexpected job status or owner")
            except (ValueError, KeyError, TypeError):
                response.failure("invalid job response")

        if self.iteration % 10 == 0:
            with self.client.get(
                "/api/jobs?limit=10&offset=0", name="GET jobs page", catch_response=True
            ) as response:
                try:
                    jobs = response.json()
                    if response.status_code != 200 or not isinstance(jobs, list) or not any(
                        job["id"] == self.job_id for job in jobs
                    ):
                        response.failure("own job missing from jobs page")
                except (ValueError, KeyError, TypeError):
                    response.failure("invalid jobs page")

        if self.iteration % 20 == 0:
            with self.client.get(
                "/api/auth/me", name="GET auth me", catch_response=True
            ) as response:
                try:
                    if (
                        response.status_code != 200
                        or response.json()["email"] != os.environ["LOAD_EMAIL"]
                    ):
                        response.failure("session expired or wrong account")
                except (ValueError, KeyError, TypeError):
                    response.failure("invalid auth response")