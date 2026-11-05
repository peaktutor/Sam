"""Start the window. An installed app has no console, so if it cannot start, say why instead of vanishing."""
import os
import traceback


def report_startup_failure(text):
    from .runner import DATA_ROOT
    path = os.path.join(DATA_ROOT, 'startup_error.log')
    try:
        os.makedirs(DATA_ROOT, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
    except OSError:
        path = None
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('P4 Analysis could not start',
                             text[-1500:] + (f'\n\nSaved to {path}' if path else ''))
        root.destroy()
    except Exception:
        pass


def show_splash():
    """The libraries take a while to load the first time: show something at once, so nobody clicks twice."""
    import tkinter as tk
    from .theme import C, enable_dpi_awareness
    enable_dpi_awareness()
    root = tk.Tk()
    root.title('P4 Analysis starting')
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    k = root.winfo_fpixels('1i') / 96
    frame = tk.Frame(root, bg=C['header'], padx=int(36 * k), pady=int(24 * k))
    frame.pack()
    tk.Label(frame, text='P4 Analysis', bg=C['header'], fg='white', font=('Segoe UI', 17, 'bold')).pack()
    tk.Label(frame, text='Starting…', bg=C['header'], fg=C['header_muted'], font=('Segoe UI', 10)).pack(pady=(4, 0))
    root.update_idletasks()
    root.geometry(f'+{(root.winfo_screenwidth() - root.winfo_reqwidth()) // 2}'
                  f'+{(root.winfo_screenheight() - root.winfo_reqheight()) // 2}')
    root.update()
    return root


try:
    splash = show_splash()
    from .gui import main
    splash.destroy()
    main()
except Exception:
    report_startup_failure(traceback.format_exc())
    raise SystemExit(1)
