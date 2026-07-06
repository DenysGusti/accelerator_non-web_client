import os

from tinydb import TinyDB


def get_db_path():
    sudo_user = os.environ.get("SUDO_USER")
    pkexec_uid = os.environ.get("PKEXEC_UID")

    if (sudo_user or pkexec_uid) and os.name != "nt":
        try:
            import pwd
            if pkexec_uid:
                home = pwd.getpwuid(int(pkexec_uid)).pw_dir
            else:
                home = pwd.getpwnam(sudo_user).pw_dir
        except Exception:
            home = os.path.expanduser("~")
    else:
        home = os.path.expanduser("~")

    token_directory = f"{home}/.accli"

    if not os.path.exists(token_directory):
        os.makedirs(token_directory)

    return f"{token_directory}/data.json"


def save_token_details(token, server_url, webcli_url):
    db_path = get_db_path()

    try:
        os.remove(db_path)
    except OSError:
        pass

    db = TinyDB(db_path)
    db.insert({
        'token': token,
        'server_url': server_url,
        'webcli_url': webcli_url
    })


def get_token():
    db_path = get_db_path()

    db = TinyDB(db_path)

    for item in db:
        token = item.get('token')
        if token:
            break

    if not token:
        print("Token does not exists. Please login.")
    return token


def get_github_app_token():
    db_path = get_db_path()

    db = TinyDB(db_path)

    for item in db:
        token = item.get('github_app_token')
        if token:
            break

    if not token:
        print("Github app token does not exists.")
    return token


def set_github_app_token(github_app_token):
    db_path = get_db_path()
    db = TinyDB(db_path)
    db.update({'github_app_token': github_app_token}, doc_ids=[1])


def set_project_slug(project_slug):
    db_path = get_db_path()
    db = TinyDB(db_path)
    db.update({'project_slug': project_slug}, doc_ids=[1])


def get_project_slug():
    db_path = get_db_path()

    db = TinyDB(db_path)

    for item in db:
        project_slug = item.get('project_slug')
        if project_slug:
            break

    if not project_slug:
        print("project slug was not set.")
    return project_slug


def get_server_url():
    db_path = get_db_path()

    db = TinyDB(db_path)

    for item in db:
        server_url = item.get('server_url')
        if server_url:
            break

    if not server_url:
        print("Server url does not exists. Please login.")
    return server_url
