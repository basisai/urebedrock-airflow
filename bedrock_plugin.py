#!/usr/bin/python3
import datetime
import json
import time
from base64 import b64encode
from http.client import HTTPSConnection

import requests
from airflow.exceptions import AirflowException
from airflow.hooks.http_hook import HttpHook
from airflow.models import BaseOperator
from airflow.plugins_manager import AirflowPlugin

API_VERSION = "v1"


class BedrockHook(HttpHook):
    def __init__(self, method, bedrock_conn_id):
        super().__init__(method=method, http_conn_id=bedrock_conn_id)
        self.conn = self.get_connection(bedrock_conn_id)
        self.base_url = f"https://{self.conn.host}"

        self.bedrock_token = None
        self.bedrock_token_expiry = None

    def get_conn(self, _headers=None):
        early_expiry = datetime.datetime.now() + datetime.timedelta(minutes=10)
        if self.bedrock_token is None or self.bedrock_token_expiry < early_expiry:
            self._authenticate()

        session = requests.Session()
        session.headers.update(
            {"Authorization": f"Bearer {self.bedrock_token}", "Content-Type": "application/json"}
        )
        return session

    def _authenticate(self):
        if self.conn.extra:
            conn = HTTPSConnection("bdrk.auth0.com")
            payload = {
                "client_id": self.conn.extra_dejson.get("client_id"),
                "client_secret": self.conn.extra_dejson.get("client_secret"),
                "audience": self.base_url,
                "grant_type": "client_credentials",
            }
            headers = {"Content-Type": "application/json"}
            conn.request("POST", "/oauth/token", json.dumps(payload), headers)
        else:
            # TODO: Remove basic auth support once we fully support oauth
            conn = HTTPSConnection(self.conn.host)
            auth_token = b64encode(f"{self.conn.login}:{self.conn.password}".encode()).decode(
                "ascii"
            )
            headers = {"Authorization": f"Basic {auth_token}"}
            conn.request("GET", "/v1/auth/login", headers=headers)

        resp = conn.getresponse()
        data = json.loads(resp.read())
        self.bedrock_token = data["access_token"]
        self.bedrock_token_expiry = datetime.datetime.now() + datetime.timedelta(
            seconds=int(data["expires_in"])
        )


class CreatePipelineOperator(BaseOperator):
    CREATE_PIPELINE_PATH = f"{API_VERSION}/pipeline/"

    def __init__(self, conn_id, name, uri, ref, username, password, **kwargs):
        super().__init__(**kwargs)
        self.conn_id = conn_id
        self.name = name
        self.uri = uri
        self.ref = ref
        self.username = username
        self.password = password

    def execute(self, context):
        hook = BedrockHook(method="POST", bedrock_conn_id=self.conn_id)
        data = json.dumps(
            {
                "name": self.name,
                "uri": self.uri,
                "ref": self.ref,
                "username": self.username,
                "password": self.password,
            }
        )

        try:
            res = hook.run(CreatePipelineOperator.CREATE_PIPELINE_PATH, data=data)
        except AirflowException as ex:
            self.log.error("Failed to create pipeline")
            raise ex

        data = json.loads(res.content)
        public_id = data["public_id"]
        self.log.info(f"Pipeline successfully created, public ID: {public_id}")
        return public_id


class RunPipelineOperator(BaseOperator):
    GET_ENVIRONMENT_PATH = f"{API_VERSION}/environment/"
    RUN_PIPELINE_PATH = f"{API_VERSION}/pipeline/{{}}/run/"
    GET_PIPELINE_RUN_PATH = f"{API_VERSION}/run/{{}}"
    WAIT_STATUS = ["Running", "Queued"]
    SUCCESS_STATUS = ["Succeeded"]

    template_fields = ("pipeline_id",)

    def __init__(self, conn_id, pipeline_id, status_poke_interval=15, **kwargs):
        super().__init__(**kwargs)
        self.conn_id = conn_id
        self.pipeline_id = pipeline_id
        self.status_poke_interval = status_poke_interval

    def execute(self, context):
        # Get the list of environments
        get_hook = BedrockHook(method="GET", bedrock_conn_id=self.conn_id)

        try:
            response = get_hook.run(RunPipelineOperator.GET_ENVIRONMENT_PATH)
        except AirflowException as ex:
            self.log.error("Failed to run pipeline")
            raise ex

        environment_id = json.loads(response.content)[0]["public_id"]

        # Run the training pipeline
        hook = BedrockHook(method="POST", bedrock_conn_id=self.conn_id)
        data = json.dumps({"environment_public_id": environment_id})

        try:
            res = hook.run(
                RunPipelineOperator.RUN_PIPELINE_PATH.format(self.pipeline_id), data=data
            )
        except AirflowException as ex:
            self.log.error("Failed to run pipeline")
            raise ex

        data = json.loads(res.content)
        pipeline_run_id = data["entity_id"]
        self.log.info(f"Pipeline successfully run, pipeline run ID: {pipeline_run_id}")

        # Poll pipeline run status
        while True:
            status = self._check_status(get_hook, pipeline_run_id)
            if status in RunPipelineOperator.WAIT_STATUS:
                time.sleep(self.status_poke_interval)
            elif status in RunPipelineOperator.SUCCESS_STATUS:
                break
            else:
                raise Exception(f"Run status is {status}")

    def _check_status(self, hook, pipeline_run_id):
        self.log.info(f"Checking status")

        try:
            res = hook.run(RunPipelineOperator.GET_PIPELINE_RUN_PATH.format(pipeline_run_id))
        except AirflowException as ex:
            self.log.error("Failed to check pipeline run status")
            raise ex

        data = json.loads(res.content)

        status = data["status"]
        self.log.info(f"Status of pipeline run: {status}")
        return status


class BedrockPlugin(AirflowPlugin):
    name = "bedrock_plugin"
    operators = [CreatePipelineOperator, RunPipelineOperator]
    hooks = [BedrockHook]