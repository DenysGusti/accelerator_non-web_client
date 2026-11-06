# Accelerator Terminal Client and Python API

This package provides:

- A **command-line client** (`accli`) for interacting with the **Accelerator**.
- A **Python API** via the `accli` package.

Both interfaces communicate with the Accelerator’s REST API, implemented in
the [Control Services Backend](https://github.com/iiasa/control_services_backend).

---

## 🔐 Authentication

This client uses **device authentication** via Auth0.

- **OAuth Flow
  **: [Device Authorization Flow](https://auth0.com/docs/get-started/authentication-and-authorization-flow/device-authorization-flow)
- **Token Validity**: 7 days
- **Access Control**: Role-Based Access Control (RBAC) with stateless tokens
- **Grants**: Limited and scoped

---

## 📖 User Guide

### ✅ Requirements

- Python >= 3.8

### 📦 Installation

#### Option 1: Standalone Binaries (Recommended)

No Python install required. Run the appropriate command for your system:

**Linux / macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/iiasa/accli/master/scripts/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/iiasa/accli/master/scripts/install.ps1 | iex
```

#### Option 2: Via PIP

```bash
pip install accli --user
```

### ▶️ Usage

#### As a Python Module

```bash
python -m accli
```

#### As an Executable

After installation, the executable might not be in your system `PATH`. You may see a warning like this:

```
WARNING: The script accli.exe is installed in 'C:\Users\<user>\AppData\Roaming\Python\Python311\Scripts' which is not on PATH.
Consider adding this directory to PATH or, if you prefer to suppress this warning, use --no-warn-script-location.
```

To resolve this, add the script's path to your system's environment variables:

- [Update PATH on Windows](https://stackoverflow.com/questions/44272416/how-to-add-a-folder-to-path-environment-variable-in-windows-10-with-screensho)
- [Update PATH on Linux](https://www.geeksforgeeks.org/how-to-set-path-permanantly-in-linux/)

> **Note:** On Linux/macOS, you may need to prefix the command with `./`, and on Windows, with `.\`.

### 🔍 Help Command

```bash
accli --help
```

**Sample Output:**

```
Usage: accli [OPTIONS] COMMAND [ARGS]...
```

### 📋 Jobs

List root jobs, newest first, with each job's type (`standalone`, `children`, `callback` or `children+callback`), its own status and the status of its whole graph:

```bash
accli jobs my-project                       # latest 10
accli jobs my-project -n 50
accli jobs my-project -s failed             # graphs with a failed job
accli jobs my-project -s processing,error
```

`--status` filters on the graph status: `processing` (or `active`), `error` (or `failed`) and `done`. A standalone job counts by its own status.

Show a job's status and details. For a job holding children or callbacks, Graph status covers the jobs below it (the same as GRAPH in `accli jobs`) and each one is listed; any job also links to the pipeline it is part of. Read a job's output with `accli logs <project> <job>`:

```bash
accli job my-project 1234
accli job my-project 1234 --json
accli job show my-project 1234
```

`accli job <project> <job>` is the same as `accli job show <project> <job>`.

Restart a job that is done or failed. The job can be a root job or a child job. The command fails if that job, a parent, or a descendant is still processing. `--rebuild` builds the container image again before the job starts.

```bash
accli job restart my-project 1234
accli job restart my-project 1234 --rebuild
```

---

## 👩‍💻 Developer Guide

### Handling self-signed certificates

Import the certificates from `accelerator_service` into the Windows Root store (auto-elevates to Administrator if needed):
```powershell
cd <path to>/accelerator_service/scripts
.\import_certs_certutil.ps1
```

### 🛠 Build & Upload

Follow the official Python packaging tutorial:  
[Packaging Projects – Python.org](https://packaging.python.org/en/latest/tutorials/packaging-projects/)

### 🚀 Release Process

1. Update the version in `accli/_version.py`
2. Tag the release:

    ```bash
    python scripts/tag.py
    ```

3. Build the package:

    ```bash
    python -m build
    ```

4. Upload to PyPI:

    ```bash
    twine upload -r pypi -u __token__ -p <password-or-token> ./dist/*
    ```

---