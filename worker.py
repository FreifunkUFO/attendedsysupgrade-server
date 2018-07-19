import threading
import glob
import re
import shutil
import urllib.request
import tempfile
import os
import os.path
import hashlib
import subprocess
import logging
import time

from utils.image import Image
from utils.common import get_hash
from utils.config import Config
from utils.database import Database

class Worker(threading.Thread):
    def __init__(self, job, params):
        self.job = job
        self.params = params
        threading.Thread.__init__(self)
        self.log = logging.getLogger(__name__)
        self.log.info("log initialized")
        self.config = Config()
        self.log.info("config initialized")
        self.database = Database(self.config)
        self.log.info("database initialized")

    def setup_meta(self):
        os.makedirs(self.params["worker"], exist_ok=True)
        self.log.debug("setup meta")
        cmdline = "git clone https://github.com/aparcar/meta-imagebuilder.git ."
        proc = subprocess.Popen(
            cmdline.split(" "),
            cwd=self.params["worker"],
            stdout=subprocess.PIPE,
            shell=False,
            stderr=subprocess.STDOUT,
        )

        output, errors = proc.communicate()
        return_code = proc.returncode

        return return_code

    def setup(self):
        self.log.debug("setup")

        return_code, output, errors = self.run_meta("download")
        if return_code == 0:
            self.log.info("setup complete")
        else:
            self.log.error("failed to download imagebuilder")
            print(output)
            print(errors)
            exit()

    # write buildlog.txt to image dir
    def store_log(self, buildlog):
        self.log.debug("write log")
        with open(self.image.params["dir"] + "/buildlog.txt", "a") as buildlog_file:
            buildlog_file.writelines(buildlog)

    # build image
    def build(self):
        self.image = Image(self.params)

        request_hash = get_hash(" ".join(self.image.as_array("package_hash")), 12)

        with tempfile.TemporaryDirectory(dir=self.config.get_folder("tempdir")) as build_dir:

            self.params["j"] = str(os.cpu_count())
            self.params["EXTRA_IMAGE_NAME"] = request_hash
            self.params["BIN_DIR"] = build_dir

            self.log.info("start build")

            return_code, buildlog, errors = self.run_meta("image")

            if return_code == 0:
                build_status = "image_created"
                # parse created manifest and add to database, returns hash of manifest file
                manifest_path = glob.glob(build_dir + "/*.manifest")[0]
                with open(manifest_path, 'r') as manifest_file:
                    manifest_content = manifest_file.read()
                    self.image.params["manifest_hash"] = get_hash(manifest_content, 15)
                    
                    manifest_pattern = r"(.+) - (.+)\n"
                    manifest_packages = dict(re.findall(manifest_pattern, manifest_content))
                    self.database.add_manifest_packages(self.image.params["manifest_hash"], manifest_packages)

                # calculate hash based on resulted manifest
                self.image.params["image_hash"] = get_hash(" ".join(self.image.as_array("manifest_hash")), 15)

                # get directory where image is stored on server
                self.image.set_image_dir()

                # create folder in advance
                os.makedirs(self.image.params["dir"], exist_ok=True)

                self.log.debug(os.listdir(build_dir))

                # move files to new location and rename contents of sha256sums
                # TODO rename request_hash to manifest_hash
                for filename in os.listdir(build_dir):
                    if os.path.exists(self.image.params["dir"] + "/" + filename):
                        break
                    shutil.move(build_dir + "/" + filename, self.image.params["dir"])

                # TODO this should be done on the worker, not client
                # however, as the request_hash is changed to manifest_hash after transer
                # it not really possible... a solution would be to only trust the server
                # and add no worker keys
                #usign_sign(os.path.join(self.store_path, "sha256sums"))
                #self.log.info("signed sha256sums")

                # possible sysupgrade names, ordered by likeliness        
                possible_sysupgrade_files = [ "*-squashfs-sysupgrade.bin",
                        "*-squashfs-sysupgrade.tar", "*-squashfs.trx",
                        "*-squashfs.chk", "*-squashfs.bin",
                        "*-squashfs-sdcard.img.gz", "*-combined-squashfs*",
                        "*.img.gz"]

                sysupgrade = None

                for sysupgrade_file in possible_sysupgrade_files:
                    sysupgrade = glob.glob(self.image.params["dir"] + "/" + sysupgrade_file)
                    if sysupgrade:
                        break

                if not sysupgrade:
                    self.log.debug("sysupgrade not found")
                    if buildlog.find("too big") != -1:
                        self.log.warning("created image was to big")
                        self.store_log(buildlog)
                        self.database.set_image_requests_status(request_hash, "imagesize_fail")
                        return False
                    else:
                        self.build_status = "no_sysupgrade"
                else:
                    self.image.params["sysupgrade"] = os.path.basename(sysupgrade[0])

                    self.store_log(buildlog)

                    self.database.add_image(self.image.get_params())
                    self.database.done_build_job(request_hash, self.image.params["image_hash"], build_status)
                    return True
            else:
                print(buildlog)
                self.log.info("build failed")
                self.database.set_image_requests_status(request_hash, 'build_fail')
 #               self.store_log(buildlog)
                return False

            self.log.info("build successfull")
    
    def run(self):
        if not os.path.exists(self.params["worker"] + "/meta"):
            if self.setup_meta():
                self.log.error("failed to setup meta ImageBuilder")
                exit()
        self.setup()
        if self.job == "image":
            self.build()
        elif self.job == "info":
            self.parse_info()
        elif self.job == "packages":
            self.parse_packages()

    def run_meta(self, cmd):
        env = os.environ.copy()
        for key, value in self.params.items():
            env[key.upper()] = value

        proc = subprocess.Popen(
            ["sh", "meta", cmd],
            cwd=self.params["worker"],
            stdout=subprocess.PIPE,
            shell=False,
            stderr=subprocess.STDOUT,
            env=env
        )

        output, errors = proc.communicate()
        return_code = proc.returncode
        output = output.decode('utf-8')

        return (return_code, output, errors)


    def parse_info(self):
        self.log.debug("parse info")

        return_code, output, errors = self.run_meta("info")

        if return_code == 0:
            default_packages_pattern = r"(.*\n)*Default Packages: (.+)\n"
            default_packages = re.match(default_packages_pattern, output, re.M).group(2)
            logging.debug("default packages: %s", default_packages)

            profiles_pattern = r"(.+):\n    (.+)\n    Packages: (.*)\n"
            profiles = re.findall(profiles_pattern, output)
            if not profiles:
                profiles = []
            self.database.insert_profiles(self.params, default_packages, profiles)
        else:
            logging.error("could not receive profiles")
            return False

    def parse_packages(self):
        self.log.info("receive packages")

        return_code, output, errors = self.run_meta("package_list")

        if return_code == 0:
            packages = re.findall(r"(.+?) - (.+?) - .*\n", output)
            self.log.info("found {} packages for {} {} {} {}".format(len(packages)))
            self.database.insert_packages_available(self.params, packages)
        else:
            self.log.warning("could not receive packages")

if __name__ == '__main__':
    config = Config()
    database = Database(config)
    while True:
        image = database.get_build_job()
        if image != None:
            job = "build"
            image["worker"] = "/tmp/worker"
            worker = Worker(job, image)
            worker.run() # TODO no threading just yet
        outdated_subtarget = database.get_subtarget_outdated()
        if outdated_subtarget:
            print(outdated_subtarget.cursor_description)
            outdated_subtarget["worker"] = "/tmp/worker"
            job = "info"
            worker = Worker(job, outdated_subtarget)
            worker.run()
            job = "packages"
            worker = Worker(job, outdated_subtarget)
            worker.run()
        time.sleep(5)

    # TODO reimplement
    #def diff_packages(self):
    #    profile_packages = self.vanilla_packages
    #    for package in self.packages:
    #        if package in profile_packages:
    #            profile_packages.remove(package)
    #    for remove_package in profile_packages:
    #        self.packages.append("-" + remove_package)

