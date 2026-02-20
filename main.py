import os
import sys
import time
import string
import subprocess
import tkinter as tk
import sqlite3
from tkinter import ttk, messagebox
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread
from queue import Queue, Empty
from enum import Enum

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Constants ---
AMOUNT_THREADS = os.cpu_count() or 4
UPDATE_INTERVAL = 50
DEFAULT_LIMIT = 100
MAX_RESULTS = 10000
INCREMENT = 100

class Color(Enum):
    WINDOW_BACKGROUND = "#1f1d1d"
    WINDOW_CONTRAST = "#3d3535"
    HEADER_GRAY = "#8A8A8A"
    DEFAULT_TEXT = "#E0E0E0"
    FILE = "#a4e6d2"    
    FOLDER = "#1bbce4"
    RESULT_TEXT = "#1a1a1a"

# --- Watchdog Handler ---
class IndexUpdateHandler(FileSystemEventHandler):
    def __init__(self, app):
        self.app = app

    def on_created(self, event):
        self.app.queue_sync_item(event.src_path)

    def on_deleted(self, event):
        self.app.queue_delete_item(event.src_path)

    def on_moved(self, event):
        self.app.queue_delete_item(event.src_path)
        self.app.queue_sync_item(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.app.queue_sync_item(event.src_path)

class DriveSearchApp:
    def __init__(self, root, initial_term="", initial_path=None):
        self.root = root

        root.title("Quick Search")
        self.root.geometry("1100x750")
        self.root.configure(bg=Color.WINDOW_BACKGROUND.value)
    
        try:
            icon_path = resource_path("QuickSearch.png")
            icon = tk.PhotoImage(file=icon_path)
            root.iconphoto(False, icon)
        except: pass

        self.results_queue = Queue()
        self.watchdog_queue = Queue()  # Queue for DB updates
        self.all_results = []
        self.executor = None
        self.searching = False
        self.indexing = False
        self.cancel_op = False
        self.lock = Lock()
        self.last_term = ""
        self.conn = None 
        self.indexed_total = 0 
        
        self.observer = None

        self.setup_styles()
        self.create_widgets()
        self.switch_drive_db()
        self.process_queue()
        
        # Start the background DB sync worker
        Thread(target=self.database_sync_worker, daemon=True).start()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        if initial_path and initial_term:
            self.executor = ThreadPoolExecutor(max_workers=AMOUNT_THREADS)
            drive_letter = initial_path[0].upper()
            if drive_letter in self.get_available_drives():
                self.init_db(drive_letter)
                
                if initial_term:
                    self.search_entry.insert(0, initial_term)
                    # Pass the full path to the search function
                    self.root.after(800, lambda: self.run_hybrid_search(
                        drive_letter, initial_term, DEFAULT_LIMIT, folder_context=initial_path
                    ))

    def init_db(self, drive_letter):
        with self.lock:
            if self.conn:
                try: self.conn.close()
                except: pass
            
            db_name = f"index_{drive_letter}.db"
            fb_path = "C:\\FastSearchDB"
            if not os.path.exists(fb_path): os.makedirs(fb_path)
            db_path = os.path.join(fb_path, db_name)

            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.conn.text_factory = lambda b: b.decode(errors='replace')
            
            self.conn.execute("PRAGMA journal_mode = WAL")  # Allows simultaneous reads/writes
            self.conn.execute("PRAGMA synchronous = NORMAL") # Recommended for WAL mode
            self.conn.execute("PRAGMA busy_timeout = 5000")  # UI waits up to 5s instead of freezing
            
            cursor = self.conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS files 
                              (type TEXT, path TEXT PRIMARY KEY, size_raw INTEGER, size_display TEXT, mtime REAL)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_path_search ON files(path)''')
            
            cursor.execute("SELECT COUNT(*) FROM files")
            self.indexed_total = cursor.fetchone()[0]
            self.conn.commit()

    # --- BATCH SYNC LOGIC ---
    def queue_sync_item(self, path):
        self.watchdog_queue.put(("UPSERT", path))

    def queue_delete_item(self, path):
        self.watchdog_queue.put(("DELETE", path))

    def database_sync_worker(self):
        """ Background thread that only writes when the app is idle """
        while True:
            # FIX: If we are searching or indexing, wait to avoid DB contention
            if self.indexing or getattr(self, 'is_searching', False):
                time.sleep(1)
                continue

            changes = []
            try:
                # Collect items from the queue
                item = self.watchdog_queue.get(timeout=1)
                changes.append(item)
                
                # Small batch window
                time.sleep(0.5) 
                while not self.watchdog_queue.empty() and len(changes) < 100:
                    changes.append(self.watchdog_queue.get())
            except Empty:
                continue

            if not self.conn: continue

            with self.lock:
                try:
                    cursor = self.conn.cursor()
                    for action, path in changes:
                        if action == "UPSERT":
                            if not os.path.exists(path): continue
                            is_f = os.path.isfile(path)
                            rtype = "File" if is_f else "Folder"
                            # We don't use the C helper here for speed; 
                            # just a quick placeholder for real-time additions
                            cursor.execute("INSERT OR REPLACE INTO files (type, path, size_raw, size_display) VALUES (?, ?, ?, ?)", 
                                           (rtype, path, -1, "..."))
                        elif action == "DELETE":
                            cursor.execute("DELETE FROM files WHERE path = ?", (path,))
                    self.conn.commit()
                except sqlite3.OperationalError:
                    # If DB is locked by a search, put items back in queue and wait
                    for c in changes: self.watchdog_queue.put(c)
                    time.sleep(2)

    def get_folder_size(self, path):
        """ Calls the C executable in 'size' mode with UTF-8 encoding """
        if self.cancel_op: return -1
        try:
            helper_exe = resource_path("scanner.exe")
            # FIX: Use .encode('utf-8') so C's MultiByteToWideChar (CP_UTF8) reads it correctly
            result = subprocess.check_output(
                [helper_exe, "size", path.encode('utf-8')],
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=15 
            )
            output = result.decode().strip()
            return int(output) if output.isdigit() else -1
        except Exception as e:
            # If path has access denied, C helper might return nothing or error
            return -1

    def start_watchdog(self, drive_path):
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join()
            except: pass
        
        self.observer = Observer()
        handler = IndexUpdateHandler(self)
        try:
            self.observer.schedule(handler, drive_path, recursive=True)
            self.observer.start()
        except Exception as e:
            print(f"Watchdog error: {e}")

    def switch_drive_db(self, event=None):
        self.cancel_op = True
        self.indexing = False
        self.searching = False
        
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
            
        drive_letter = self.drive_combo.get()
        drive_path = f"{drive_letter}:\\"
        
        self.init_db(drive_letter)
        self.cancel_op = False 
        self.status_label.config(text=f"Drive {drive_letter}: {self.indexed_total} items indexed")
        
        Thread(target=self.start_watchdog, args=(drive_path,), daemon=True).start()

    def sync_single_item(self, path):
        try:
            if not os.path.exists(path): return
            stat = os.stat(path)
            is_file = os.path.isfile(path)
            rtype = "File" if is_file else "Folder"
            raw_sz = stat.st_size if is_file else -1
            disp_sz = self.format_size(raw_sz) if raw_sz != -1 else "..."
            mtime = stat.st_mtime
            
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)", 
                               (rtype, path, raw_sz, disp_sz, mtime))
                self.conn.commit()
                # Update status count subtly
                self.indexed_total += 1
        except: pass

    def delete_single_item(self, path):
        try:
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute("DELETE FROM files WHERE path = ?", (path,))
                cursor.execute("DELETE FROM files WHERE path LIKE ?", (path + "\\%",))
                self.conn.commit()
        except: pass

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TFrame", background=Color.WINDOW_BACKGROUND.value)
        style.configure("TLabel", background=Color.WINDOW_BACKGROUND.value, foreground=Color.DEFAULT_TEXT.value)
        style.configure("Treeview", background=Color.WINDOW_BACKGROUND.value, 
                        foreground=Color.DEFAULT_TEXT.value, fieldbackground=Color.WINDOW_BACKGROUND.value, borderwidth=0)
        style.configure("Treeview.Heading", background=Color.HEADER_GRAY.value, foreground="white", font=('Segoe UI', 10, 'bold'))
        style.map("Treeview.Heading", background=[('active', Color.FOLDER.value)])
        style.configure("Search.Horizontal.TProgressbar", thickness=10, troughcolor=Color.WINDOW_CONTRAST.value, background=Color.FILE.value)

    def create_widgets(self):
        top_frame = ttk.Frame(self.root)
        top_frame.pack(pady=10, padx=10, fill="x")

        ttk.Label(top_frame, text="Search:").pack(side="left")
        self.search_entry = tk.Entry(top_frame, width=25, bg=Color.WINDOW_CONTRAST.value, 
                                     fg="white", insertbackground="white", relief="flat")
        self.search_entry.pack(side="left", padx=5)
        self.search_entry.bind("<Return>", lambda e: self.start_search())

        ttk.Label(top_frame, text="Drive:").pack(side="left", padx=(5, 0))
        self.drive_combo = ttk.Combobox(top_frame, values=self.get_available_drives(), state="readonly", width=5)
        self.drive_combo.pack(side="left", padx=5)
        if self.drive_combo["values"]: self.drive_combo.current(0)
        self.drive_combo.bind("<<ComboboxSelected>>", self.switch_drive_db)

        self.limit_var = tk.IntVar(value=DEFAULT_LIMIT)
        self.limit_spin = tk.Spinbox(top_frame, from_=10, to=MAX_RESULTS, increment=INCREMENT, 
                                     textvariable=self.limit_var, width=7, bg=Color.WINDOW_CONTRAST.value, 
                                     fg="white")
        self.limit_spin.pack(side="left", padx=5)

        self.search_button = ttk.Button(top_frame, text="Search", command=self.start_search)
        self.search_button.pack(side="left", padx=5)
        self.index_button = ttk.Button(top_frame, text="Update Index", command=self.start_indexing)
        self.index_button.pack(side="left", padx=5)
        self.stop_button = ttk.Button(top_frame, text="Stop", command=self.stop_operations)
        self.stop_button.pack(side="left", padx=5)

        mid_frame = ttk.Frame(self.root)
        mid_frame.pack(pady=5, padx=10, fill="x")
        
        # Primary Sort Filter
        ttk.Label(mid_frame, text="Sort:").pack(side="left")
        self.sort_option = tk.StringVar(value="Relevance")
        sort_choices = ["Relevance", "Name (A-Z)", "Name (Z-A)", "Size (Large)", "Size (Small)"]
        self.sort_menu = ttk.OptionMenu(mid_frame, self.sort_option, sort_choices[0], *sort_choices, command=self.apply_manual_sort)
        self.sort_menu.pack(side="left", padx=5)

        # Secondary Type Filter (New)
        ttk.Label(mid_frame, text="Type:").pack(side="left", padx=(10, 0))
        self.type_filter = tk.StringVar(value="Mixed")
        type_choices = ["Mixed", "Files First", "Folders First"]
        self.type_menu = ttk.OptionMenu(mid_frame, self.type_filter, type_choices[0], *type_choices, command=self.apply_manual_sort)
        self.type_menu.pack(side="left", padx=5)

        self.progress = ttk.Progressbar(mid_frame, orient="horizontal", length=300, mode="determinate", style="Search.Horizontal.TProgressbar")
        self.progress.pack(side="right", padx=10)
        self.status_label = ttk.Label(mid_frame, text="Ready")
        self.status_label.pack(side="right")

        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.tree = ttk.Treeview(tree_frame, columns=("Type", "Size", "Path"), show="headings")
        for col in ["Type", "Size", "Path"]:
            self.tree.heading(col, text=col)
        self.tree.column("Type", width=80); self.tree.column("Size", width=120); self.tree.column("Path", width=800)
        self.tree.tag_configure("File", background=Color.FILE.value, foreground=Color.RESULT_TEXT.value)
        self.tree.tag_configure("Folder", background=Color.FOLDER.value, foreground=Color.RESULT_TEXT.value)
        
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_selected)

    def start_indexing(self):
        if self.indexing: return
        self.indexing = True
        self.cancel_op = False
        self.index_button.config(state="disabled")
        self.progress["value"] = 0
        drive = f"{self.drive_combo.get()}:\\"
        
        # We use a standard Thread for the main process control to keep UI responsive
        Thread(target=self.run_full_indexing_process, args=(drive,), daemon=True).start()

    def run_full_indexing_process(self, drive_path):
        db_name = f"index_{self.drive_combo.get()}.db"
        db_path = os.path.join("C:\\FastSearchDB", db_name)
        helper_exe = resource_path("scanner.exe")

        self.scan_count = 0 
        try:
            process = subprocess.Popen(
                [helper_exe, drive_path, db_path],
                stdout=subprocess.PIPE,
                text=True,
                encoding='utf-8', 
                errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            for line in process.stdout:
                if self.cancel_op:
                    process.terminate()
                    break
                
                line = line.strip()
                if line.startswith("COUNT|"):
                    count = line.split("|")[1]
                    self.root.after(0, lambda c=count: self.status_label.config(text=f"Searching: {c} items..."))
                
                elif line.startswith("FINAL_COUNT|"):
                    self.scan_count = int(line.split("|")[1])
                    self.root.after(0, self.progress.stop)
                    self.root.after(0, lambda: self.progress.configure(mode="determinate"))

                elif line.startswith("PROGRESS|"):
                    current = int(line.split("|")[1])
                    if self.scan_count > 0:
                        percent = (current / self.scan_count) * 100
                        self.root.after(0, lambda p=percent, c=current: self.update_index_ui_from_c(p, c))
                
                elif line.startswith("STATUS|"):
                    msg = line.split("|")[1]
                    self.root.after(0, lambda m=msg: self.status_label.config(text=m))

            process.wait()
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"C Helper failed: {e}"))
        
        # Reload database connection to reflect C changes
        self.init_db(self.drive_combo.get()) 
        self.indexing = False
        self.root.after(0, self.finish_indexing)

    def finish_indexing(self):
        self.index_button.config(state="normal")
        self.progress["value"] = 0
        self.status_label.config(text=f"Index Updated: {self.indexed_total} items")

    def update_index_ui_from_c(self, percent, count):
        self.progress["value"] = percent
        self.status_label.config(text=f"Indexing: {count} / {self.scan_count}")

    def start_search(self):
        self.stop_operations(search_only=True)
        term = self.search_entry.get().strip()
        if not term: return
        self.last_term = term
        self.searching = True
        self.cancel_op = False
        self.all_results = []
        self.tree.delete(*self.tree.get_children())
        self.search_button.config(text="Searching...")
        self.executor = ThreadPoolExecutor(max_workers=AMOUNT_THREADS)
        self.executor.submit(self.run_hybrid_search, self.drive_combo.get(), term, self.limit_var.get())

    def run_hybrid_search(self, drive, term, limit, folder_context=None):
        # Prevent the worker crash by flagging search state
        self.is_searching = True 
        try:
            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute("PRAGMA busy_timeout = 2000")
                
                search_pattern = f"%{term}%"
                
                if folder_context:
                    # Search ONLY within the specific folder context
                    # Path must start with folder_context AND contain the term
                    folder_pattern = f"{folder_context}%"
                    query = "SELECT type, size_display, path, size_raw FROM files WHERE path LIKE ? AND path LIKE ? LIMIT ?"
                    cursor.execute(query, (folder_pattern, search_pattern, limit))
                else:
                    # Global drive search
                    query = "SELECT type, size_display, path, size_raw FROM files WHERE path LIKE ? LIMIT ?"
                    cursor.execute(query, (search_pattern, limit))
                
                rows = cursor.fetchall()
        except Exception as e:
            print(f"Search error: {e}")
            rows = []
        finally:
            self.is_searching = False

        if rows:
            with self.lock:
                self.all_results = [list(r) for r in rows]
            
            # Apply sorting/filtering (Files first/Folders first etc)
            self.apply_manual_sort()
            
            # Start folder sizing for results
            for i, row in enumerate(self.all_results):
                if row[0] == "Folder":
                    # Use path-based sizing to avoid the index race condition
                    self.executor.submit(self.async_folder_size, row[2], row[2])
            
            self.root.after(0, lambda: self.search_button.config(text="Search"))
        else:
            # If nothing in DB, fall back to live scan of that specific folder
            start_dir = folder_context if folder_context else f"{drive}:\\"
            self.run_live_scan(start_dir, term, limit)

    def async_folder_size(self, _, path): # Discard the index, use path
        size = self.get_folder_size(path)
        disp = self.format_size(size)
        # Pass the path as the identifier
        self.results_queue.put(("UPDATE", path, disp, size))

    def process_queue(self):
        try:
            while True:
                data = self.results_queue.get_nowait()
                if data[0] == "UPDATE":
                    _, folder_path, disp, raw = data # Changed 'idx' to 'folder_path'
                    with self.lock:
                        # Find the item in our data list by path, not index
                        for i, res in enumerate(self.all_results):
                            if res[2] == folder_path:
                                self.all_results[i][1] = disp
                                self.all_results[i][3] = raw
                                break
                    
                    # Update the Treeview row visually
                    for item in self.tree.get_children():
                        if self.tree.item(item)['values'][2] == folder_path:
                            self.tree.set(item, column="Size", value=disp)
                            break
                else:
                    idx, item = data
                    self.tree.insert("", "end", values=(item[0], item[1], item[2]), tags=(item[0],))
        except Empty: pass
        self.root.after(UPDATE_INTERVAL, self.process_queue)

    def get_folder_size(self, path):
        try:
            helper_exe = resource_path("scanner.exe")
            # Call C helper in 'size' mode
            result = subprocess.check_output(
                [helper_exe, "size", path],
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5 # Safety timeout
            )
            return int(result.decode().strip())
        except Exception as e:
            print(f"C Size Error: {e}")
            return -1

    def format_size(self, size_bytes):
        if size_bytes < 0: return "..."
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024: return f"{size_bytes:.1f}{unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f}TB"

    def stop_operations(self, search_only=False):
        self.cancel_op = True
        self.searching = False
        if not search_only:
            self.indexing = False
        if self.executor: self.executor.shutdown(wait=False, cancel_futures=True)
        self.search_button.config(text="Search")
        self.index_button.config(state="normal")

    def apply_manual_sort(self, *args):
        """ Runs sorting in a background thread to prevent UI freezing """
        if not self.all_results: return
        self.status_label.config(text="Sorting...")
        Thread(target=self._sort_and_refresh_task, daemon=True).start()

    def _sort_and_refresh_task(self):
        mode = self.sort_option.get()
        priority = self.type_filter.get()
        
        with self.lock:
            # Step 1: Primary Sort
            if mode == "Relevance":
                self.all_results.sort(key=lambda x: (self.score(x[2], self.last_term), x[2].lower()))
            elif mode == "Name (A-Z)":
                self.all_results.sort(key=lambda x: os.path.basename(x[2]).lower())
            elif mode == "Name (Z-A)":
                self.all_results.sort(key=lambda x: os.path.basename(x[2]).lower(), reverse=True)
            elif mode == "Size (Large)":
                self.all_results.sort(key=lambda x: x[3], reverse=True)
            elif mode == "Size (Small)":
                self.all_results.sort(key=lambda x: (x[3] == -1, x[3]))

            # Step 2: Secondary Priority Filter
            if priority == "Files First":
                self.all_results.sort(key=lambda x: (x[0] != "File"))
            elif priority == "Folders First":
                self.all_results.sort(key=lambda x: (x[0] != "Folder"))
            
            # Create a snapshot of data to avoid locking during UI insertion
            data_to_show = list(self.all_results)
        
        # Start chunked insertion on main thread
        self.root.after(0, lambda: self.refresh_ui_chunked(data_to_show))
    
    def score(self, path, term):
        name = os.path.basename(path).lower()
        t = term.lower()
        return 0 if name == t else (1 if name.startswith(t) else 2)

    def refresh_ui_chunked(self, data, index=0):
        """ Inserts results in chunks of 100 to prevent UI freezing """
        if index == 0:
            self.tree.delete(*self.tree.get_children())
            self.status_label.config(text="Updating list...")

        chunk = data[index:index + 100]
        for item in chunk:
            self.tree.insert("", "end", values=(item[0], item[1], item[2]), tags=(item[0],))

        if index + 100 < len(data):
            # Schedule next chunk with 1ms delay to allow UI events to process
            self.root.after(1, lambda: self.refresh_ui_chunked(data, index + 100))
        else:
            self.status_label.config(text=f"Showing {len(data)} results")

    def run_live_scan(self, drive_path, term, limit):
        try:
            top_items = os.listdir(drive_path)
            for item in top_items:
                if self.cancel_op: break
                full_path = os.path.join(drive_path, item)
                if os.path.isdir(full_path): self.executor.submit(self.live_scan_worker, full_path, term, limit)
                elif term.lower() in item.lower(): self.add_live_result("File", full_path)
        except: pass

    def live_scan_worker(self, path, term, limit):
        term_lower = term.lower()
        stack = [path]
        while stack:
            if self.cancel_op: break
            with self.lock:
                if len(self.all_results) >= limit: break
            
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        if self.cancel_op: break
                        if term_lower in entry.name.lower():
                            is_file = entry.is_file()
                            rtype = "File" if is_file else "Folder"
                            try:
                                stat = entry.stat()
                                raw_size = stat.st_size if is_file else -1
                                disp_size = self.format_size(raw_size)
                                res = [rtype, disp_size, entry.path, raw_size]
                                with self.lock:
                                    if len(self.all_results) < limit:
                                        idx = len(self.all_results)
                                        self.all_results.append(res)
                                        self.results_queue.put((idx, res))
                                        if not is_file: self.executor.submit(self.async_folder_size, idx, entry.path)
                            except: continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
            except: continue

    def add_live_result(self, rtype, rpath):
        try:
            stat = os.stat(rpath)
            raw_size = stat.st_size if rtype == "File" else -1
            disp_size = self.format_size(raw_size)
            res = [rtype, disp_size, rpath, raw_size]
            with self.lock:
                idx = len(self.all_results)
                self.all_results.append(res)
            self.results_queue.put((idx, res))
        except: pass

    def get_available_drives(self):
        return [l for l in string.ascii_uppercase if os.path.exists(f"{l}:/")]

    def open_selected(self, event):
        item_id = self.tree.focus()
        if item_id:
            path = self.tree.item(item_id)["values"][2]
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')

    def on_close(self):
        self.cancel_op = True
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join()
            except: pass
        if self.conn:
            self.conn.close()
        self.root.destroy()

def center_on_cursor(window, width, height):
    # Hide window and update to get accurate screen info
    window.withdraw()
    window.update_idletasks()

    # Get cursor position
    mouse_x = window.winfo_pointerx()
    mouse_y = window.winfo_pointery()

    # Get screen dimensions
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()

    # Calculate target position (offset slightly by 10px so cursor isn't on the edge)
    start_x = mouse_x + 10
    start_y = mouse_y + 10

    # Boundary check: Ensure window doesn't go off the right edge
    if start_x + width > screen_w:
        start_x = screen_w - width - 20
        
    # Boundary check: Ensure window doesn't go off the bottom edge
    if start_y + height > screen_h:
        start_y = screen_h - height - 40 # Extra padding for taskbar

    # Ensure it doesn't go off the left/top if cursor is near 0,0
    start_x = max(0, start_x)
    start_y = max(0, start_y)

    # Apply the geometry and show the window
    window.geometry(f"{width}x{height}+{start_x}+{start_y}")
    window.deiconify()

def resource_path(filename):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.abspath("."), filename)

if __name__ == "__main__":
    root = tk.Tk()

    center_on_cursor(root, 1100, 750)

    # Check for command line arguments
    initial_term = ""
    initial_path = None
    
    argv = sys.argv
    # If triggered via 'sch keyword', sys.argv[1] will be the keyword
    if len(sys.argv) > 1:
        initial_term = sys.argv[1]
        # File Explorer passes the current folder as the Working Directory
        initial_path = os.getcwd()

    app = DriveSearchApp(root, initial_term=initial_term, initial_path=initial_path)
    root.mainloop()