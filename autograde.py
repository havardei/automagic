import json
import logging
import pathlib
import time
import base64
import os
from datetime import datetime

import requests
import urllib3

log = logging.getLogger(__name__)


def get_token():
    """boilerplate: get token from share file.

    Make sure to start jupyterhub in this directory first
    """
    here = pathlib.Path(__file__).parent
    token_file = here.joinpath("service-token")
    log.info(f"Loading token from {token_file}")
    with token_file.open("r") as f:
        token = f.read().strip()
    return token


def make_session(token):
    """Create a requests.Session with our service token in the Authorization header"""
    session = requests.Session()
    session.headers = {"Authorization": f"token {token}"}
    session.verify = False
    return session


def event_stream(session, url):
    """Generator yielding events from a JSON event stream

    For use with the server progress API
    """
    r = session.get(url, stream=True)
    r.raise_for_status()
    for line in r.iter_lines():
        line = line.decode("utf8", "replace")
        # event lines all start with `data:`
        # all other lines should be ignored (they will be empty)
        if line.startswith("data:"):
            yield json.loads(line.split(":", 1)[1])


def start_server(session, hub_url, user, server_name=""):
    """Start a server for a jupyterhub user

    Returns the full URL for accessing the server
    """
    user_url = f"{hub_url}/hub/api/users/{user}"
    log_name = f"{user}/{server_name}".rstrip("/")

    # step 1: get user status
    r = session.get(user_url)
    r.raise_for_status()
    user_model = r.json()

    # if server is not 'active', request launch
    if server_name not in user_model.get("servers", {}):
        log.info(f"Starting server {log_name}")
        r = session.post(f"{user_url}/servers/{server_name}")
        r.raise_for_status()
        if r.status_code == 201:
            log.info(f"Server {log_name} is launched and ready")
        elif r.status_code == 202:
            log.info(f"Server {log_name} is launching...")
        else:
            log.warning(f"Unexpected status: {r.status_code}")
        r = session.get(user_url)
        r.raise_for_status()
        user_model = r.json()

    # report server status
    server = user_model["servers"][server_name]
    if server["pending"]:
        status = f"pending {server['pending']}"
    elif server["ready"]:
        status = "ready"
    else:
        # shouldn't be possible!
        raise ValueError(f"Unexpected server state: {server}")

    log.info(f"Server {log_name} is {status}")

    # wait for server to be ready using progress API
    progress_url = user_model["servers"][server_name]["progress_url"]
    for event in event_stream(session, f"{hub_url}{progress_url}"):
        log.info(f"Progress {event['progress']}%: {event['message']}")
        if event.get("ready"):
            server_url = event["url"]
            break
    else:
        # server never ready
        raise ValueError(f"{log_name} never started!")

    # at this point, we know the server is ready and waiting to receive requests
    # return the full URL where the server can be accessed
    return f"{hub_url}{server_url}"


def stop_server(session, hub_url, user, server_name=""):
    """Stop a server via the JupyterHub API

    Returns when the server has finished stopping
    """
    # step 1: get user status
    user_url = f"{hub_url}/hub/api/users/{user}"
    server_url = f"{user_url}/servers/{server_name}"
    log_name = f"{user}/{server_name}".rstrip("/")

    log.info(f"Stopping server {log_name}")
    r = session.delete(server_url)
    if r.status_code == 404:
        log.info(f"Server {log_name} already stopped")

    r.raise_for_status()
    if r.status_code == 204:
        log.info(f"Server {log_name} stopped")
        return

    # else: 202, stop requested, but not complete
    # wait for stop to finish
    log.info(f"Server {log_name} stopping...")
    # wait for server to be done stopping
    while True:
        r = session.get(user_url)
        r.raise_for_status()
        user_model = r.json()
        if server_name not in user_model.get("servers", {}):
            log.info(f"Server {log_name} stopped")
            return
        server = user_model["servers"][server_name]
        if not server["pending"]:
            raise ValueError(f"Waiting for {log_name}, but no longer pending.")
        log.info(f"Server {log_name} pending: {server['pending']}")
        # wait to poll again
        time.sleep(1)


def make_notebook_session(session, hub_url, user):
    user_url = f"{hub_url}/hub/api/users/{user}"
    tokens_url = f"{user_url}/tokens"
    r = session.post(tokens_url)
    token = r.json().get("token")
    return make_session(token)


def create_terminal(session, hub_url, user):

    terminal_url = f"{hub_url}/user/{user}/api/terminals"

    r = session.post(terminal_url)

    if r.status_code == 200:
        print("Successfully created a new terminal.")
        print(r.json())
    else:
        print("Failed creating a terminal.")


def clear_old_terminals(session, hub_url, user, threshold_in_s=1800):
    terminal_url = f"{hub_url}/user/{user}/api/terminals"
    r = session.get(terminal_url)

    if r.status_code == 200:
        print(f"Clearing terminals older than {threshold_in_s} seconds.")
        t1 = datetime.utcnow()
        for t in r.json():
            t2 = datetime.strptime(t["last_activity"], "%Y-%m-%dT%H:%M:%S.%fZ")
            if (t1 - t2).seconds > threshold_in_s:
                r = session.delete(f"{terminal_url}/{t['name']}")
    else:
        print("Failed clearing old terminals.")


def create_files(session, hub_url, user):
    file_url = f"{hub_url}/user/{user}/api/contents"

    filename = ".profile"
    path = f"/home/jovyan/{filename}"

    data = {
        "path": path,
        "name": filename,
        "content": "python automagic.py",
        "type": "file",
        "format": "text",
    }

    r = session.put(f"{file_url}/{filename}", data=json.dumps(data))

    if r.status_code == 200:
        print(f"Succesfully created file: {path}")
        print(r.json())
    else:
        print(f"Failed creating file: {path}")

    with open("magic.py", "rb") as magic:
        b64data = base64.b64encode(magic.read())
        filename = "automagic.py"
        path = f"/home/jovyan/{filename}"

        data = {
            "path": path,
            "name": filename,
            "content": b64data.decode("utf-8"),
            "type": "file",
            "format": "base64",
        }

        r = session.put(f"{file_url}/{filename}", data=json.dumps(data))

        if r.status_code == 200:
            print(f"Succesfully created file: {path}")
            print(r.json())
        else:
            print(f"Failed creating file: {path}")


def main():
    token = os.environ.get("HUB_TOKEN", "123thisisnotarealtokenbeepboop321")
    user = os.environ.get("HUB_USER")
    hub_url = os.environ.get("HUB_URL")

    # session = make_session(get_token())
    session = make_session(token)
    server_url = start_server(session, hub_url, user)
    r = session.get(f"{server_url}/api/status")
    r.raise_for_status()
    log.info(f"Server status: {r.text}")

    nb_session = make_notebook_session(session, hub_url, user)

    clear_old_terminals(nb_session, hub_url, user)
    create_files(nb_session, hub_url, user)
    create_terminal(nb_session, hub_url, user)

    # time.sleep(2700)
    # stop_server(session, hub_url, user)


if __name__ == "__main__":
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    logging.basicConfig(level=logging.INFO)
    main()
