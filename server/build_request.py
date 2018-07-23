import os
import logging
import glob
from http import HTTPStatus
from flask import Response

from utils.image import Image
from server.request import Request
from utils.common import get_hash

class BuildRequest(Request):
    def __init__(self, config, db):
        super().__init__(config, db)

    def _process_request(self):
        self.log.debug("request_json: %s", self.request_json)
        # if request_hash is available check the database directly
        if "request_hash" in self.request_json:
            self.request = self.database.check_build_request_hash(self.request_json["request_hash"])

            if not self.request:
                self.response_status = HTTPStatus.NOT_FOUND
                return self.respond()
            else:
                return self.return_status()
        else:
            # required params for a build request
            missing_params = self.check_missing_params(["distro", "version", "target", "subtarget", "board"])
            if missing_params:
                return self.respond()

        self.request_json["profile"] = self.request_json["board"] # TODO fix this workaround

        # create image object to get the request_hash
        image = Image(self.request_json)
        image.set_packages_hash()
        request_hash = get_hash(" ".join(image.as_array("packages_hash")), 12)
        self.request = self.database.check_build_request_hash(request_hash)

        # if found return instantly the status
        if self.request:
            self.log.debug("found image in database: %s", self.request["status"])
            return self.return_status()
        else:
            self.request["request_hash"] = request_hash

        self.request["packages_hash"] = image.params["packages_hash"] # TODO make this better

        # if not perform various checks to see if the request is acutally valid
        # check for valid distro and version
        bad_request = self.check_bad_request()
        if bad_request:
            return bad_request

        # check for valid target and subtarget
        bad_target = self.check_bad_target()
        if bad_target:
            return bad_target

        # check for existing packages
        bad_packages = self.check_bad_packages()
        if bad_packages:
            return bad_packages

        # add package_hash to database
        self.database.insert_packages_hash(self.request["packages_hash"], self.request["packages"])

        # now some heavy guess work is done to figure out the profile
        # eventually this could be simplified if upstream unifirm the profiles/boards
        # TODO not yet working
        if "board" in self.request_json:
            self.log.debug("board in request, search for %s", self.request_json["board"])
            self.request["profile"] = self.database.check_profile(self.request["distro"], self.request["version"], self.request["target"], self.request["subtarget"], self.request_json["board"])

        if not self.request["profile"]:
            if "model" in self.request_json:
                self.log.debug("model in request, search for %s", self.request_json["model"])
                self.request["profile"] = self.database.check_model(self.request["distro"], self.request["version"], self.request["target"], self.request["subtarget"], self.request_json["model"])
                self.log.debug("model search found profile %s", self.request["profile"])

        if not self.request["profile"]:
            if self.database.check_profile(self.request["distro"], self.request["version"], self.request["target"], self.request["subtarget"], "Generic"):
                self.request["profile"] = "Generic"
            elif self.database.check_profile(self.request["distro"], self.request["version"], self.request["target"], self.request["subtarget"], "generic"):
                self.request["profile"] = "generic"
            else:
                self.response_json["error"] = "unknown device, please check model and board params"
                self.response_status = HTTPStatus.PRECONDITION_FAILED # 412
                return self.respond()

        # all checks passed, eventually add to queue!
        self.request.pop("packages")
        self.database.add_build_job(self.request)
        return self.return_queued()

    def return_queued(self):
        self.response_header["X-Imagebuilder-Status"] = "queue"
        self.response_header['X-Build-Queue-Position'] = '1337' # TODO: currently not implemented
        self.response_json["request_hash"] = self.request["request_hash"]

        self.response_status = HTTPStatus.ACCEPTED # 202
        return self.respond()

    def return_status(self):
        # image created, return all desired information
        if self.request["status"] == "created":
            file_path, sysupgrade_file = self.database.get_sysupgrade(self.request["image_hash"])
            self.response_json["sysupgrade"] = "{}/static/{}{}".format(self.config.get("server"), file_path, sysupgrade_file)
            self.response_json["log"] = "{}/static/{}/buildlog.txt".format(self.config.get("server"), file_path)
            self.response_json["files"] =  "{}/json/{}".format(self.config.get("server"), file_path)
            self.response_json["request_hash"] = self.request["request_hash"]
            self.response_json["image_hash"] = self.request["image_hash"]

            self.response_status = HTTPStatus.OK # 200

        elif self.request["status"] == "no_sysupgrade":
            if self.sysupgrade_requested:
                # no sysupgrade found but requested, let user figure out what to do
                self.response_json["error"] = "No sysupgrade file produced, may not supported by modell."

                self.response_status = HTTPStatus.NOT_IMPLEMENTED # 501
            else:
                # no sysupgrade found but not requested, factory image is likely from interest
                file_path = self.database.get_image_path(image_hash)
                self.response_json["files"] =  "{}/json/{}".format(self.config.get("server"), file_path)
                self.response_json["log"] = "{}/static/{}build-{}.log".format(self.config.get("server"), file_path, image_hash)
                self.response_json["request_hash"] = request_hash
                self.response_json["image_hash"] = image_hash

                self.response_status = HTTPStatus.OK # 200

            self.respond()

        # image request passed validation and is queued
        elif self.request["status"] == "requested":
            self.return_queued()

        # image is currently building
        elif self.request["status"] == "building":
            self.response_header["X-Imagebuilder-Status"] = "building"
            self.response_json["request_hash"] = self.request["request_hash"]

            self.response_status = HTTPStatus.ACCEPTED # 202

        # build failed, see build log for details
        elif self.request["status"] == "build_fail":
            self.response_json["error"] = "imagebuilder faild to create image"
            self.response_json["log"] = "{}/static/faillogs/faillog-{}.txt".format(self.config.get("server"), self.request["request_hash"])
            self.response_json["request_hash"] = self.request["request_hash"]

            self.response_status = HTTPStatus.INTERNAL_SERVER_ERROR # 500

        # likely to many package where requested
        elif self.request["status"] == "imagesize_fail":
            self.response_json["error"] = "No firmware created due to image size. Try again with less packages selected."
            self.response_json["log"] = "{}/static/faillogs/request-{}.log".format(self.config.get("server"), request_hash)
            self.response_json["request_hash"] = request_hash

            self.response_status = 413 # PAYLOAD_TO_LARGE RCF 7231

        # something happend with is not yet covered in here
        else:
            self.response_json["error"] = self.request["status"]

            self.response_status = HTTPStatus.INTERNAL_SERVER_ERROR

        return self.respond()
