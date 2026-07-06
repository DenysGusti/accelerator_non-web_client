import os
import sys
import json
import time
import platform
import threading
import subprocess
from pathlib import Path

def setup_tcl_tk_env():
    base = sys.base_prefix
    for sub in ["tcl", "lib", "."]:
        search_dir = os.path.join(base, sub)
        if not os.path.isdir(search_dir):
            continue
        try:
            for item in os.listdir(search_dir):
                item_path = os.path.join(search_dir, item)
                if os.path.isdir(item_path):
                    if item.startswith("tcl") and not os.environ.get("TCL_LIBRARY"):
                        if os.path.isfile(os.path.join(item_path, "init.tcl")):
                            os.environ["TCL_LIBRARY"] = item_path
                    elif item.startswith("tk") and not os.environ.get("TK_LIBRARY"):
                        if os.path.isfile(os.path.join(item_path, "tk.tcl")):
                            os.environ["TK_LIBRARY"] = item_path
        except Exception:
            pass

setup_tcl_tk_env()

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Ensure PyInstaller static analyzer traces and bundles all CLI dependencies
import accli.cli

class AccliGuiApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IIASA Accelerator Client (accli)")
        self.geometry("800x600")
        self.minsize(700, 500)
        
        # Apply dark modern theme style
        self.style = ttk.Style()
        self.configure_styles()
        
        # Main layout
        self.create_widgets()
        
        # Start state check poll
        self.refresh_login_status()
        self.refresh_mounts_list()
        
    def configure_styles(self):
        self.configure(bg="#1e1e1e")
        self.style.theme_use("clam")
        
        # Style Definitions
        self.style.configure(".", background="#1e1e1e", foreground="#ffffff", font=("Segoe UI", 10))
        self.style.configure("TFrame", background="#1e1e1e")
        self.style.configure("TLabel", background="#1e1e1e", foreground="#ffffff")
        self.style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#007acc")
        
        # Notebook Style
        self.style.configure("TNotebook", background="#252526", borderwidth=0)
        self.style.configure("TNotebook.Tab", background="#2d2d2d", foreground="#888888", padding=[15, 5])
        self.style.map("TNotebook.Tab",
            background=[("selected", "#252526"), ("active", "#333333")],
            foreground=[("selected", "#ffffff"), ("active", "#ffffff")]
        )
        
        # Buttons Style
        self.style.configure("TButton", background="#007acc", foreground="#ffffff", borderwidth=0, padding=[10, 5])
        self.style.map("TButton",
            background=[("active", "#0098ff"), ("disabled", "#555555")],
            foreground=[("disabled", "#aaaaaa")]
        )
        self.style.configure("Stop.TButton", background="#e51400")
        self.style.map("Stop.TButton",
            background=[("active", "#ff3333"), ("disabled", "#555555")]
        )
        self.style.configure("Refresh.TButton", background="#333333")
        self.style.map("Refresh.TButton", background=[("active", "#444444")])
        
        # Entry fields
        self.style.configure("TEntry", fieldbackground="#2d2d2d", foreground="#ffffff", borderwidth=0)
        
        # Checkboxes
        self.style.configure("TCheckbutton", background="#1e1e1e", foreground="#ffffff")
        self.style.map("TCheckbutton", background=[("active", "#1e1e1e")])

    def create_widgets(self):
        # Top banner
        banner_frame = ttk.Frame(self, padding=10)
        banner_frame.pack(fill=tk.X)
        
        title_label = ttk.Label(banner_frame, text="IIASA ACCELERATOR CLIENT", style="Header.TLabel")
        title_label.pack(side=tk.LEFT)
        
        self.status_bar_val = tk.StringVar(value="Checking state...")
        status_lbl = ttk.Label(banner_frame, textvariable=self.status_bar_val, font=("Segoe UI", 9, "italic"), foreground="#4ec9b0")
        status_lbl.pack(side=tk.RIGHT)
        
        # Tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
        # Build Tabs
        self.build_mounts_tab()
        self.build_copy_tab()
        self.build_dispatch_tab()
        self.build_auth_tab()
        self.build_logs_tab()

    def run_cli_async(self, args, on_done=None, log_to_viewer=True, elevate=False):
        """Runs the CLI binary in a background thread to prevent UI freezing."""
        def worker():
            # Determine how to invoke accli
            if getattr(sys, "frozen", False):
                # Executable mode (PyInstaller package)
                cmd = [sys.executable] + args
            else:
                # Script mode
                cmd = [sys.executable, "-m", "accli.cli"] + args

            if elevate and platform.system() != "Windows":
                import shutil
                if shutil.which("pkexec"):
                    cmd = ["pkexec"] + cmd
                else:
                    cmd = ["sudo"] + cmd

            try:
                if log_to_viewer:
                    self.append_log(f"Executing: {' '.join(cmd)}\n")
                
                # Start process with hidden console window on Windows
                startupinfo = None
                if platform.system() == "Windows":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    startupinfo=startupinfo
                )
                
                output_lines = []
                while True:
                    line = process.stdout.readline()
                    if not line:
                        break
                    output_lines.append(line)
                    if log_to_viewer:
                        self.append_log(line)
                
                process.wait()
                
                if on_done:
                    self.after(0, lambda: on_done(process.returncode, "".join(output_lines)))
            except Exception as e:
                self.append_log(f"ERROR executing command: {e}\n")
                if on_done:
                    self.after(0, lambda: on_done(-1, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def append_log(self, text):
        self.after(0, lambda: self._safe_append_log(text))

    def _safe_append_log(self, text):
        self.log_txt.configure(state=tk.NORMAL)
        self.log_txt.insert(tk.END, text)
        self.log_txt.see(tk.END)
        self.log_txt.configure(state=tk.DISABLED)

    # ------------------ AUTH TAB ------------------
    def build_auth_tab(self):
        auth_tab = ttk.Frame(self.notebook, padding=20)
        self.notebook.add(auth_tab, text="Connection / Login")
        
        # Login Details Card
        card = ttk.LabelFrame(auth_tab, text="Login Credentials", padding=15)
        card.pack(fill=tk.X, pady=10)
        
        ttk.Label(card, text="Server URL:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.server_url_val = tk.StringVar(value="https://accelerator.iiasa.ac.at")
        self.server_entry = ttk.Entry(card, textvariable=self.server_url_val, width=40)
        self.server_entry.grid(row=0, column=1, sticky=tk.W, padx=10, pady=5)
        
        self.login_btn = ttk.Button(card, text="Authenticate / Login", command=self.action_login)
        self.login_btn.grid(row=0, column=2, padx=10, pady=5)
        
        # Connection status details
        self.conn_details_txt = tk.Text(auth_tab, height=12, bg="#2d2d2d", fg="#ffffff", insertbackground="white", font=("Courier New", 10), state=tk.DISABLED)
        self.conn_details_txt.pack(fill=tk.BOTH, expand=True, pady=10)

    def refresh_login_status(self):
        from accli.token import get_db_path
        from tinydb import TinyDB
        db_path = get_db_path()
        logged_in = False
        server_url = "https://accelerator.iiasa.ac.at"
        
        if os.path.exists(db_path):
            try:
                db = TinyDB(db_path)
                for item in db:
                    if item.get('token'):
                        logged_in = True
                    if item.get('server_url'):
                        server_url = item.get('server_url')
            except Exception:
                pass
                
        self.is_logged_in_state = logged_in
        if logged_in:
            self.login_btn.configure(text="Logout", style="Stop.TButton")
            self.server_entry.configure(state=tk.DISABLED)
            self.server_url_val.set(server_url)
            self.status_bar_val.set("Session Status: Connected")
        else:
            self.login_btn.configure(text="Authenticate / Login", style="TButton")
            self.server_entry.configure(state=tk.NORMAL)
            self.status_bar_val.set("Session Status: Disconnected")
            
        def on_check_done(code, output):
            self.conn_details_txt.configure(state=tk.NORMAL)
            self.conn_details_txt.delete("1.0", tk.END)
            self.conn_details_txt.insert(tk.END, output)
            self.conn_details_txt.configure(state=tk.DISABLED)

        self.run_cli_async(["status"], on_done=on_check_done, log_to_viewer=False)

    def action_login(self):
        if hasattr(self, "is_logged_in_state") and self.is_logged_in_state:
            from accli.token import get_db_path
            db_path = get_db_path()
            try:
                if os.path.exists(db_path):
                    os.remove(db_path)
                self.append_log("Logged out successfully.\n")
                messagebox.showinfo("Success", "Logged out successfully.")
            except Exception as e:
                self.append_log(f"Error during logout: {e}\n")
            self.refresh_login_status()
            return

        url = self.server_url_val.get().strip()
        if not url:
            messagebox.showerror("Error", "Please provide a valid Server URL.")
            return
        
        self.status_bar_val.set("Authenticating...")
        
        import webbrowser
        from tkinter import simpledialog
        import requests
        from accli.token import save_token_details
        
        webcli_url = url
        auth_url = f"{webcli_url.rstrip('/')}/acli-auth-code"
        
        self.append_log(f"Opening browser for authentication at: {auth_url}\n")
        try:
            webbrowser.open(auth_url)
        except Exception as e:
            self.append_log(f"Warning: Could not open browser automatically: {e}\n")
            
        auth_code = simpledialog.askstring(
            "Authorization Code", 
            f"Please authenticate in your browser at:\n{auth_url}\n\nThen enter the authorization code here:"
        )
        
        if not auth_code:
            self.status_bar_val.set("Session Status: Disconnected")
            return
            
        def perform_auth():
            try:
                self.append_log("Exchanging authorization code for token...\n")
                token_endpoint = f"{url.rstrip('/')}/api/v1/oauth/device/token/"
                response = requests.post(
                    token_endpoint,
                    json={"device_authorization_code": auth_code.strip()},
                    verify=True
                )
                
                if response.status_code == 400:
                    detail = response.json().get("detail", "Unknown error")
                    self.append_log(f"Authentication Error: {detail}\n")
                    self.after(0, lambda: messagebox.showerror("Auth Error", f"Failed to log in: {detail}"))
                    self.after(0, self.refresh_login_status)
                    return
                    
                response.raise_for_status()
                data = response.json()
                
                save_token_details(data, url, webcli_url)
                self.append_log("Authentication successful! Token saved.\n")
                self.after(0, lambda: messagebox.showinfo("Success", "Successfully logged in!"))
                self.after(0, self.refresh_login_status)
            except Exception as e:
                self.append_log(f"Authentication Failed: {e}\n")
                self.after(0, lambda: messagebox.showerror("Error", f"Authentication failed: {e}"))
                self.after(0, self.refresh_login_status)

        threading.Thread(target=perform_auth, daemon=True).start()

    # ------------------ DISPATCH TAB ------------------
    def build_dispatch_tab(self):
        dispatch_tab = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(dispatch_tab, text="Task Dispatcher")
        
        card = ttk.LabelFrame(dispatch_tab, text="Workflow Configuration", padding=15)
        card.pack(fill=tk.X, pady=10)
        
        # Project Slug
        ttk.Label(card, text="Project Slug:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.disp_slug_val = tk.StringVar()
        ttk.Entry(card, textvariable=self.disp_slug_val, width=30).grid(row=0, column=1, sticky=tk.W, padx=10, pady=5)
        
        # Root Task Variable
        ttk.Label(card, text="Root Task Variable:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.disp_root_val = tk.StringVar()
        ttk.Entry(card, textvariable=self.disp_root_val, width=30).grid(row=1, column=1, sticky=tk.W, padx=10, pady=5)
        
        # Workflow File Path
        ttk.Label(card, text="Workflow Python File (-f):").grid(row=2, column=0, sticky=tk.W, pady=5)
        file_frame = ttk.Frame(card)
        file_frame.grid(row=2, column=1, sticky=tk.W, padx=10, pady=5)
        
        self.disp_file_val = tk.StringVar(value="wkube.py")
        ttk.Entry(file_frame, textvariable=self.disp_file_val, width=30).pack(side=tk.LEFT)
        ttk.Button(file_frame, text="Browse...", command=self.browse_workflow_file).pack(side=tk.LEFT, padx=5)
        
        # Dispatch Button
        btn_frame = ttk.Frame(dispatch_tab)
        btn_frame.pack(fill=tk.X, pady=15)
        
        dispatch_btn = ttk.Button(btn_frame, text="Dispatch Task", command=self.action_dispatch)
        dispatch_btn.pack(anchor=tk.CENTER)
        
        # Try to pre-populate project slug from config
        try:
            from accli.token import get_project_slug
            slug = get_project_slug()
            if slug:
                self.disp_slug_val.set(slug)
        except Exception:
            pass

    def browse_workflow_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("Python Files", "*.py")])
        if file_path:
            self.disp_file_val.set(file_path)

    def action_dispatch(self):
        slug = self.disp_slug_val.get().strip()
        root_var = self.disp_root_val.get().strip()
        wf_file = self.disp_file_val.get().strip()
        
        if not slug or not root_var or not wf_file:
            messagebox.showerror("Error", "Please fill in all Dispatch fields.")
            return
            
        args = ["dispatch", slug, root_var, "-f", wf_file]
        
        self.status_bar_val.set("Dispatching workflow...")
        self.notebook.select(self.log_tab_idx)
        
        def on_done(code, output):
            if code == 0:
                self.status_bar_val.set("Workflow Dispatched")
                messagebox.showinfo("Success", "Workflow task successfully dispatched!")
            else:
                self.status_bar_val.set("Dispatch Failed")
                messagebox.showerror("Error", "Failed to dispatch workflow task. See logs for details.")

        self.run_cli_async(args, on_done=on_done)

    # ------------------ MOUNTS TAB ------------------
    def build_mounts_tab(self):
        mounts_tab = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(mounts_tab, text="Mount Manager")
        
        # Left Panel (Input Form)
        form_frame = ttk.LabelFrame(mounts_tab, text="Mount Configuration", padding=15)
        form_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        
        ttk.Label(form_frame, text="Project Slug / Repository ID:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.proj_slug_val = tk.StringVar()
        ttk.Entry(form_frame, textvariable=self.proj_slug_val, width=25).grid(row=0, column=1, sticky=tk.W, padx=10, pady=5)
        
        ttk.Label(form_frame, text="Mount Point / Drive Letter:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.mount_point_val = tk.StringVar(value="W:" if platform.system() == "Windows" else "")
        ttk.Entry(form_frame, textvariable=self.mount_point_val, width=25).grid(row=1, column=1, sticky=tk.W, padx=10, pady=5)
        
        # Options
        ttk.Label(form_frame, text="Options:").grid(row=2, column=0, sticky=tk.NW, pady=5)
        options_frame = ttk.Frame(form_frame)
        options_frame.grid(row=2, column=1, sticky=tk.W, padx=10, pady=5)
        
        self.opt_overlay = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Enable Overlay (local writes)", variable=self.opt_overlay).pack(anchor=tk.W, pady=2)
        
        self.opt_readonly = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Force Read-Only", variable=self.opt_readonly).pack(anchor=tk.W, pady=2)
        
        self.opt_fuse = tk.BooleanVar(value=False)
        if platform.system() != "Windows":
            ttk.Checkbutton(options_frame, text="Use FUSE backend", variable=self.opt_fuse).pack(anchor=tk.W, pady=2)
            
        # Mount / Unmount buttons
        btn_frame = ttk.Frame(form_frame)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=15, sticky=tk.W)
        
        mount_btn = ttk.Button(btn_frame, text="Start Mount", command=self.action_mount_start)
        mount_btn.pack(side=tk.LEFT, padx=5)
        
        # Right Panel (List of mounts)
        list_frame = ttk.LabelFrame(mounts_tab, text="Active Mounts", padding=10)
        list_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        list_btn_frame = ttk.Frame(list_frame)
        list_btn_frame.pack(fill=tk.X, pady=5)
        
        refresh_btn = ttk.Button(list_btn_frame, text="Refresh", style="Refresh.TButton", command=self.refresh_mounts_list)
        refresh_btn.pack(side=tk.LEFT, padx=2)
        
        stop_btn = ttk.Button(list_btn_frame, text="Stop Selected", style="Stop.TButton", command=self.action_mount_stop)
        stop_btn.pack(side=tk.RIGHT, padx=2)
        
        self.mounts_list = tk.Listbox(list_frame, bg="#2d2d2d", fg="#ffffff", selectbackground="#007acc", font=("Segoe UI", 10))
        self.mounts_list.pack(fill=tk.BOTH, expand=True)

    def refresh_mounts_list(self):
        def on_done(code, output):
            self.mounts_list.delete(0, tk.END)
            # Filter and parse 'accli mount status' output
            lines = output.splitlines()
            for line in lines:
                cleaned = line.strip()
                if not cleaned:
                    continue
                
                # Only insert actual mount entries, skip headers and info logs
                if platform.system() == "Windows":
                    # Look for lines starting with drive letter (e.g. W:)
                    if len(cleaned) >= 2 and cleaned[0].isalpha() and cleaned[1] == ":":
                        self.mounts_list.insert(tk.END, cleaned)
                else:
                    # Look for lines starting with absolute/home path (e.g. / or ~)
                    if cleaned.startswith("/") or cleaned.startswith("~"):
                        self.mounts_list.insert(tk.END, cleaned)
        
        self.run_cli_async(["mount", "status"], on_done=on_done, log_to_viewer=False)

    def action_mount_start(self):
        slug = self.proj_slug_val.get().strip()
        point = self.mount_point_val.get().strip()
        if not slug:
            messagebox.showerror("Error", "Please enter a valid Project Slug.")
            return
            
        args = ["mount", "start", slug]
        if point:
            args.append(point)
        if self.opt_overlay.get():
            args.append("--overlay")
        if self.opt_readonly.get():
            args.append("--read-only")
        if platform.system() != "Windows" and self.opt_fuse.get():
            args.append("--fuse")
            
        self.status_bar_val.set("Mounting project...")
        self.notebook.select(self.log_tab_idx)
        
        def on_done(code, output):
            self.refresh_mounts_list()
            if code == 0:
                if "system restart is required" in output.lower() or "restart your computer" in output.lower():
                    self.status_bar_val.set("Restart Required")
                    messagebox.showwarning(
                        "System Restart Required",
                        "A Windows system restart is required for the new NFS Client features and registry policies to take effect.\n\n"
                        "Please restart your computer, then run the mount again."
                    )
                else:
                    self.status_bar_val.set("Mount Active")
                    messagebox.showinfo("Success", f"Successfully mounted project '{slug}'!")
            else:
                self.status_bar_val.set("Mount Failed")
                messagebox.showerror("Error", f"Failed to mount project '{slug}'. See logs for details.")

        self.run_cli_async(args, on_done=on_done, elevate=True)

    def action_mount_stop(self):
        selected = self.mounts_list.get(tk.ACTIVE)
        if not selected:
            messagebox.showwarning("Warning", "Please select a mount to stop from the list.")
            return
            
        # Parse the mount point from the status line
        # E.g. "W:       \\\\127.0.0.1\\!" -> "W:"
        # Or parse folder paths
        parts = selected.split()
        if not parts:
            return
            
        mount_point = parts[0]
        args = ["mount", "stop", mount_point]
        
        self.status_bar_val.set("Stopping mount...")
        self.notebook.select(self.log_tab_idx)
        
        def on_done(code, output):
            self.refresh_mounts_list()
            if code == 0:
                self.status_bar_val.set("Mount Stopped")
                messagebox.showinfo("Success", f"Successfully stopped mount at {mount_point}.")
            else:
                self.status_bar_val.set("Stop Failed")
                messagebox.showerror("Error", f"Failed to stop mount at {mount_point}.")

        self.run_cli_async(args, on_done=on_done, elevate=True)

    # ------------------ COPY TAB ------------------
    def build_copy_tab(self):
        copy_tab = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(copy_tab, text="File Transfer")
        
        # Source Group
        src_frame = ttk.LabelFrame(copy_tab, text="Source Path", padding=10)
        src_frame.pack(fill=tk.X, pady=5)
        self.src_val = tk.StringVar()
        ttk.Entry(src_frame, textvariable=self.src_val).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(src_frame, text="Browse...", command=lambda: self.browse_path(self.src_val)).pack(side=tk.RIGHT)
        
        # Swap button
        swap_frame = ttk.Frame(copy_tab)
        swap_frame.pack(fill=tk.X)
        ttk.Button(swap_frame, text="<-> Swap Paths", style="Refresh.TButton", command=self.swap_paths).pack(pady=2)

        # Destination Group
        dest_frame = ttk.LabelFrame(copy_tab, text="Destination Path", padding=10)
        dest_frame.pack(fill=tk.X, pady=5)
        self.dest_val = tk.StringVar()
        ttk.Entry(dest_frame, textvariable=self.dest_val).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(dest_frame, text="Browse...", command=lambda: self.browse_path(self.dest_val)).pack(side=tk.RIGHT)
        
        # Copy Action
        btn_frame = ttk.Frame(copy_tab)
        btn_frame.pack(fill=tk.X, pady=15)
        ttk.Button(btn_frame, text="Start Transfer (Copy)", command=self.action_copy).pack(anchor=tk.CENTER)

        # Tip
        tip_lbl = ttk.Label(copy_tab, text="Tip: Remote paths must be structured as 'acc://[project-slug]/[file-or-folder]'.\nLocal paths can be chosen using the Browse button.", font=("Segoe UI", 9, "italic"), justify=tk.CENTER)
        tip_lbl.pack(pady=10)

    def browse_path(self, var_target):
        # Open file dialog
        path = filedialog.askopenfilename() or filedialog.askdirectory()
        if path:
            var_target.set(path)

    def swap_paths(self):
        s = self.src_val.get()
        d = self.dest_val.get()
        self.src_val.set(d)
        self.dest_val.set(s)

    def action_copy(self):
        src = self.src_val.get().strip()
        dest = self.dest_val.get().strip()
        if not src or not dest:
            messagebox.showerror("Error", "Please fill in both Source and Destination paths.")
            return
            
        args = ["copy", src, dest]
        
        self.status_bar_val.set("Transferring files...")
        self.notebook.select(self.log_tab_idx)
        
        def on_done(code, output):
            if code == 0:
                self.status_bar_val.set("Transfer Completed")
                messagebox.showinfo("Success", "File transfer finished successfully!")
            else:
                self.status_bar_val.set("Transfer Failed")
                messagebox.showerror("Error", "File transfer failed. See logs for details.")

        self.run_cli_async(args, on_done=on_done)

    # ------------------ LOGS TAB ------------------
    def build_logs_tab(self):
        logs_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(logs_tab, text="Log Console")
        self.log_tab_idx = self.notebook.index(logs_tab)
        
        # Log buttons
        ctrl_frame = ttk.Frame(logs_tab)
        ctrl_frame.pack(fill=tk.X, pady=(0, 5))
        
        clear_btn = ttk.Button(ctrl_frame, text="Clear Console", style="Refresh.TButton", command=self.clear_logs)
        clear_btn.pack(side=tk.LEFT)
        
        self.log_txt = tk.Text(logs_tab, bg="#1e1e1e", fg="#d4d4d4", insertbackground="white", font=("Courier New", 10), state=tk.DISABLED)
        self.log_txt.pack(fill=tk.BOTH, expand=True)

    def clear_logs(self):
        self.log_txt.configure(state=tk.NORMAL)
        self.log_txt.delete("1.0", tk.END)
        self.log_txt.configure(state=tk.DISABLED)

def main():
    app = AccliGuiApp()
    app.mainloop()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from accli.cli import app as cli_app
        cli_app()
    else:
        main()
