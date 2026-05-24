// MCPReactor — .NET ManagedReactor for autocad-mcp event log.
//
// Commands registered after NETLOAD:
//   MCPATTACHREACTOR — start recording.
//   MCPDETACHREACTOR — stop recording.
//   MCPREACTORSTATUS — print current state to the AutoCAD command line.
//
// Output: appends one JSON-Lines record per event to
//   <AUTOCAD_MCP_IPC_DIR>/autocad_mcp_events.log
// matching the schema the LISP reactor produces so the Python `EventTail`
// reader works against either source.
//
// Threading: AutoCAD raises all reactor events on its main UI thread —
// callbacks must return quickly and must not call back into the AutoCAD
// API from another thread. We do only a tiny amount of read-only work
// (read entity DXF name / layer / handle) inline and write to a file
// through a lock-protected StreamWriter.

using System;
using System.Globalization;
using System.IO;
using System.Text;

using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
// `Autodesk.AutoCAD.Runtime.Exception` shadows `System.Exception` — pull
// only the attribute types we need to avoid a wholesale namespace import.
using AcRuntime = Autodesk.AutoCAD.Runtime;

using CommandMethodAttribute = Autodesk.AutoCAD.Runtime.CommandMethodAttribute;
using CommandClassAttribute = Autodesk.AutoCAD.Runtime.CommandClassAttribute;

[assembly: CommandClass(typeof(AutocadMcp.Reactor.ReactorCommand))]

namespace AutocadMcp.Reactor;

public static class ReactorCommand
{
    private static ManagedReactor? _active;

    [CommandMethod("MCPATTACHREACTOR")]
    public static void Attach()
    {
        Document doc = Application.DocumentManager.MdiActiveDocument;
        Editor ed = doc.Editor;

        if (_active is { Attached: true })
        {
            ed.WriteMessage("\nMCP: reactor already attached.\n");
            return;
        }

        try
        {
            string logPath = ResolveLogPath();
            _active = new ManagedReactor(logPath, ed);
            _active.Attach(doc);
            ed.WriteMessage($"\nMCP reactor attached — writing to {logPath}\n");
        }
        catch (Exception ex)
        {
            ed.WriteMessage($"\nMCP: attach failed: {ex.Message}\n");
        }
    }

    [CommandMethod("MCPDETACHREACTOR")]
    public static void Detach()
    {
        Document doc = Application.DocumentManager.MdiActiveDocument;
        Editor ed = doc.Editor;
        if (_active is null || !_active.Attached)
        {
            ed.WriteMessage("\nMCP: no reactor attached.\n");
            return;
        }
        try
        {
            _active.Detach();
            ed.WriteMessage("\nMCP reactor detached.\n");
        }
        catch (Exception ex)
        {
            ed.WriteMessage($"\nMCP: detach failed: {ex.Message}\n");
        }
        finally
        {
            _active = null;
        }
    }

    [CommandMethod("MCPREACTORSTATUS")]
    public static void Status()
    {
        Editor ed = Application.DocumentManager.MdiActiveDocument.Editor;
        if (_active is null)
        {
            ed.WriteMessage("\nMCP reactor: not attached.\n");
            return;
        }
        ed.WriteMessage(
            $"\nMCP reactor: attached={_active.Attached}, " +
            $"events_written={_active.EventsWritten}, " +
            $"log={_active.LogPath}\n");
    }

    /// <summary>
    /// Resolve the event-log path the same way the LISP reactor and the
    /// Python `events.py` module do — `AUTOCAD_MCP_IPC_DIR` env var
    /// falling back to `<tempdir>/autocad-mcp`. Returns the file path,
    /// not the directory.
    /// </summary>
    private static string ResolveLogPath()
    {
        string? envDir = Environment.GetEnvironmentVariable("AUTOCAD_MCP_IPC_DIR");
        string dir = string.IsNullOrWhiteSpace(envDir)
            ? Path.Combine(Path.GetTempPath(), "autocad-mcp")
            : envDir!;
        Directory.CreateDirectory(dir);
        return Path.Combine(dir, "autocad_mcp_events.log");
    }
}
