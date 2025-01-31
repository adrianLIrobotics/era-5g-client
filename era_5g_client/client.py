import logging
import os
from collections.abc import Callable
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests
from requests import HTTPError, Response

from era_5g_client.client_base import NetAppClientBase
from era_5g_client.dataclasses import MiddlewareInfo, NetAppLocation, MiddlewarePlanInfo, MiddlewareActionInfo, MiddlewareRosTopicModel, MiddlewareServiceInfo
from era_5g_client.exceptions import FailedToConnect, NetAppNotReady
from era_5g_client.middleware_resource_checker import MiddlewareResourceChecker

buffer: List[Tuple[np.ndarray, Optional[str]]] = []

# port of the netapp's server
NETAPP_PORT = int(os.getenv("NETAPP_PORT", 5896))


class RunTaskMode(Enum):
    # deploy the task but don't wait until it is ready, do not register with it
    DO_NOTHING = 1
    # wait until the netapp is ready, do not register with it
    WAIT = 2
    # wait until the netapp is ready and register with it afterwards
    WAIT_AND_REGISTER = 3


class NetAppClient(NetAppClientBase):
    """Extension of the NetAppClientBase class, which enable communication with
    the Middleware.

    It allows to deploy the NetApp and check on the status of the NetApp
    """

    def __init__(
        self,
        results_event: Callable,
        image_error_event: Optional[Callable] = None,
        json_error_event: Optional[Callable] = None,
    ) -> None:
        """Constructor.

        Args:
            results_event (Callable): callback where results will arrive
            image_error_event (Callable, optional): Callback which is emitted when server
                failed to process the incoming image.
            json_error_event (Callable, optional): Callback which is emitted when server
                failed to process the incoming json data.

        Raises:
            FailedToConnect: When connection to the middleware could not be set or
                login failed
            FailedToObtainPlan: When the plan was not successfully returned from
                the middleware
        """

        super().__init__(results_event, image_error_event, json_error_event)

        self.host: Optional[str] = None
        self.action_plan_id: Optional[str] = None
        self.resource_checker: Optional[MiddlewareResourceChecker] = None
        self.middleware_info: Optional[MiddlewareInfo] = None
        self.token: Optional[str] = None

    def connect_to_middleware(self, middleware_info: MiddlewareInfo) -> None:
        """Authenticates with the middleware and obtains a token for future
        calls.

        Args:
            middleware_info (MiddlewareInfo): Middleware info, i.e. dataclass with address, user's id and password

        Raises:
            FailedToConnect: Raised when the authentication with the
                middleware failed
        """
        self.middleware_info = middleware_info
        self.middleware_info.address = self.middleware_info.address.rstrip("/")
        try:
            # connect to the middleware
            self.token = self.gateway_login(self.middleware_info.user_id, self.middleware_info.password)
        except FailedToConnect as ex:
            logging.error(f"Can't connect to middleware: {ex}")
            raise

    def run_task(
        self,
        task_id: str,
        robot_id: str,
        resource_lock: bool,
        mode: Optional[RunTaskMode] = RunTaskMode.WAIT_AND_REGISTER,
        gstreamer: Optional[bool] = False,
        ws_data: Optional[bool] = False,
        args: Optional[Dict] = None,
    ) -> None:
        """Deploys the task with provided *task_id* using middleware and
        (optionally) waits until the netapp is ready and register with it.

        Args:
            task_id (str): The GUID of the task which should be deployed.
            robot_id (str): The GUID of the robot that deploys the NetApp.
            resource_lock (bool): TBA
            mode (Optional[RunTaskMode]): Specify the mode in which the run_task
                works
            gstreamer (Optional[bool], optional):  Indicates if a GStreamer pipeline
                should be initialized for image transport. Applied only if register
                is True. Defaults to False.
            ws_data (Optional[bool], optional): Indicates if a separate websocket channel
                for data transport should be set. Applied only if register
                is True. Defaults to False.
            args (Optional[Dict], optional): NetApp-specific arguments. Applied only if register
                is True. Defaults to None.

        Raises:
            FailedToConnect: Raised when running the task failed.
        """
        assert self.middleware_info
        try:
            middlewarePlan = self.gateway_get_plan(
                task_id, resource_lock, robot_id
            )  # Get the plan_id by sending the token and task_id
            
            self.action_plan_id = middlewarePlan["ActionPlanId"]

            if not self.action_plan_id:
                raise FailedToConnect("Failed to obtain action plan id...")

            self.resource_checker = MiddlewareResourceChecker(
                str(self.token),
                self.action_plan_id,
                self.middleware_info.build_api_endpoint("orchestrate/orchestrate/plan"),
                daemon=True,
            )

            self.resource_checker.start()
            if mode in [RunTaskMode.WAIT, RunTaskMode.WAIT_AND_REGISTER]:
                self.wait_until_netapp_ready()
                self.load_netapp_address()
                if not self.netapp_location:
                    raise FailedToConnect("Failed to obtain NetApp URI or port")
                if mode == RunTaskMode.WAIT_AND_REGISTER:
                    self.register(self.netapp_location, gstreamer, ws_data, args)
        except (FailedToConnect, NetAppNotReady) as ex:
            self.delete_all_resources()
            logging.error(f"Failed to run task: {ex}")
            raise

    def register(
        self,
        netapp_location: NetAppLocation,
        gstreamer: Optional[bool] = False,
        ws_data: Optional[bool] = False,
        args: Optional[Dict] = None,
    ) -> Response:
        """Calls the /register endpoint of the NetApp interface and if the
        registration is successful, it sets up the WebSocket connection for
        results retrieval.

        Args:
            netapp_location (NetAppLocation): The URI and port of the NetApp interface.
            gstreamer (Optional[bool], optional):  Indicates if a GStreamer pipeline
                should be initialized for image transport. Defaults to False.
            ws_data (Optional[bool], optional): Indicates if a separate websocket channel
                for data transport should be set. Defaults to False.
            args (Optional[Dict], optional): NetApp-specific arguments. Defaults to None.

        Raises:
            NetAppNotReady: Raised when register called before the NetApp is ready.

        Returns:
            Response: The response from the NetApp.
        """

        if not self.resource_checker:
            raise NetAppNotReady("Not connected to the middleware.")

        if not self.resource_checker.is_ready():
            raise NetAppNotReady("Not ready.")

        response = super().register(netapp_location, gstreamer, ws_data, args)

        return response

    def disconnect(self) -> None:
        """Calls the /unregister endpoint of the server and disconnects the
        WebSocket connection."""
        super().disconnect()
        if self.resource_checker is not None:
            self.resource_checker.stop()
        self.delete_all_resources()

    def wait_until_netapp_ready(self) -> None:
        """Blocking wait until the NetApp is running.

        Raises:
            NetAppNotReady: _description_
        """
        if not self.resource_checker:
            raise FailedToConnect("Not connected to middleware.")
        self.resource_checker.wait_until_resource_ready()

    def load_netapp_address(self) -> None:
        if not (self.resource_checker and self.resource_checker.is_ready()):
            raise NetAppNotReady
        self.netapp_location = NetAppLocation(str(self.resource_checker.url), NETAPP_PORT)

    def gateway_login(self, user_id: str, password: str) -> str:
        assert self.middleware_info
        print("Trying to log into the middleware")
        # Request Login
        try:
            r = requests.post(
                self.middleware_info.build_api_endpoint("Login"), json={"Id": user_id, "Password": password}
            )
            response = r.json()
            if "errors" in response:
                raise FailedToConnect(str(response["errors"]))
            new_token = response["token"]  # Token is stored here
            # time.sleep(10)
            if not isinstance(new_token, str) or not new_token:
                raise FailedToConnect("Invalid token.")
            return new_token

        except requests.HTTPError as e:
            raise FailedToConnect(f"Could not login to the middleware gateway, status code: {e.response.status_code}")
        except KeyError as e:
            raise FailedToConnect(f"Could not login to the middleware gateway, the response does not contain {e}")
            
    def parser_middleware_plan_info(self, response: dict) -> MiddlewarePlanInfo:
        actionSequenceData = response['ActionSequence']
        action_list: list = []
        for action in actionSequenceData:
            service_list: list = []
            serviceData = action["services"]
            for service in serviceData:
                rosTopicsPub = []
                rosTopicsSub = []
                topicPubData = service["rosTopicsPub"]
                topicSubData = service["rosTopicsSub"]
                for topic in topicPubData:
                    rosTopicsPub.append(MiddlewareRosTopicModel(topic["name"],topic["type"],topic["description"],topic["enabled"] ))
                
                for topic in topicPubData:
                    rosTopicsSub.append(MiddlewareRosTopicModel(topic["name"],topic["type"],topic["description"],topic["enabled"] ))

                service_list.append(MiddlewareServiceInfo(service["id"], service["name"],service["serviceInstanceId"],service["ServiceType"],service["isReusable"],service["desiredStatus"],
                                      service["serviceUrl"],topicPubData,topicSubData, service["rosVersion"], service["rosDistro"], service["tags"],
                                      service["instanceFamily"],service["successRate"],service["serviceStatus"], service["containerImage"], service["minimunRam"], service["minimumNumCores"],
                                      service["onboardedTime"]))
                
            action_list.append(MiddlewareActionInfo(action["id"], action["name"], action["tags"], action["order"], action["placement"], action["placementType"], action["actionPriority"], action["actionStatus"],service_list ))

        return MiddlewarePlanInfo(response["id"], response["name"], response["ReplanActionPlannerLocked"], response["ResourceLock"], response["TaskPriority"],
                                     response["ActionPlanId"], response["FullReplan"], response["PartialRePlan"], action_list)

    
    def gateway_get_plan(self, taskid: str, resource_lock: bool, robot_id: str) -> str:
        assert self.middleware_info
        # Request plan

        try:
            print("Goal task is: " + str(taskid))
            hed = {"Authorization": f"Bearer {str(self.token)}"}
            data = {
                "TaskId": str(taskid),
                "LockResourceReUse": resource_lock,
                "RobotId": robot_id,
            }
            response = requests.post(
                self.middleware_info.build_api_endpoint("Task/Plan"), json=data, headers=hed
            ).json()
            if not isinstance(response, dict):
                raise FailedToConnect("Invalid response.")

            if "statusCode" in response and (response["statusCode"] == 500 or response["statusCode"] == 400):
                raise FailedToConnect(f"response {response['statusCode']}: {response['message']}")
            # todo:             if "errors" in response:
            #                 raise FailedToConnect(str(response["errors"]))
            
            return  self.parser_middleware_plan_info(response)
           
        except KeyError as e:
            raise FailedToConnect(f"Could not get the plan: {e}")
            
            
    def delete_all_resources(self) -> None:
        if self.token is None or self.action_plan_id is None:
            return

        try:
            hed = {"Authorization": "Bearer " + str(self.token)}
            if self.middleware_info:
                url = self.middleware_info.build_api_endpoint(
                    f"orchestrate/orchestrate/plan/{str(self.action_plan_id)}"
                )
                response = requests.delete(url, headers=hed)

                if response.ok:
                    print("Resource deleted")

        except HTTPError as e:
            print(e.response.status_code)
            raise FailedToConnect("Error, could not get delete the resource, revisit the log files for more details.")

    def delete_single_resource(self) -> None:
        raise NotImplementedError  # TODO

    def gateway_log_off(self) -> None:
        print("Middleware log out successful")
        # TODO
        pass
