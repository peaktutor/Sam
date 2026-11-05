// P4 Analysis.exe: starts the window with the Python bundled next to it. Compiled by build_installer.py.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(dir, "runtime", "pythonw.exe");
        if (!File.Exists(python))
        {
            MessageBox.Show("Could not find the bundled Python:\n" + python + "\n\nPlease reinstall P4 Analysis.",
                            "P4 Analysis", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        var start = new ProcessStartInfo(python, "-m p4_app")
        {
            WorkingDirectory = dir,
            UseShellExecute = false
        };
        // another Python's settings on this PC must not leak into the bundled one
        start.EnvironmentVariables.Remove("PYTHONPATH");
        start.EnvironmentVariables.Remove("PYTHONHOME");
        Process.Start(start);
        return 0;
    }
}
