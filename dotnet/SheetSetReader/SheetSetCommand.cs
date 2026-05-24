// MCPREADDST — read a binary .dst sheet set and write its contents as JSON.
//
// Usage from AutoCAD:
//   Command: NETLOAD
//   Pick: C:\path\to\SheetSetReader.dll
//   Command: MCPREADDST
//   Source .dst file: C:\...\IRD_Addition.dst
//   JSON output path: C:\...\out.json
//
// Or via the IPC protocol the Python COM backend uses:
//   doc.SendCommand("(command \"MCPREADDST\" \"path/to.dst\" \"path/to.json\") ")
//
// Output schema (single JSON document, UTF-8, no BOM):
//   {
//     "ok": true,
//     "name": "...",
//     "sheets": [
//       {"number": "...", "title": "...", "description": "...",
//        "dwg_file": "...", "layout": "..."},
//       ...
//     ]
//   }
//
// On error: {"ok": false, "error": "...", "category": "..."}
//
// "category" values mirror the Python backend categories so a Python
// caller can map them onto its CommandResult.category field directly.

using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;

using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.EditorInput;
// `Autodesk.AutoCAD.Runtime` collides with `System.Exception`. Pull only
// the attribute types we need explicitly to avoid `using` it wholesale.
using AcRuntime = Autodesk.AutoCAD.Runtime;
using Autodesk.AutoCAD.Interop;

using CommandMethodAttribute = Autodesk.AutoCAD.Runtime.CommandMethodAttribute;
using CommandClassAttribute = Autodesk.AutoCAD.Runtime.CommandClassAttribute;

// Make the type available to AutoCAD's command-method resolver. Without
// this, NETLOAD finds the assembly but doesn't auto-register the command.
[assembly: CommandClass(typeof(AutocadMcp.SheetSetReader.SheetSetCommand))]

namespace AutocadMcp.SheetSetReader;

public static class SheetSetCommand
{
    [CommandMethod("MCPREADDST")]
    public static void ReadDst()
    {
        Document doc = Application.DocumentManager.MdiActiveDocument;
        Editor ed = doc.Editor;

        // Prompt for the .dst path. Using string prompts so the command
        // is scriptable from `SendCommand`: each prompt consumes one
        // whitespace-delimited token from the input stream.
        var dstRes = ed.GetString(new PromptStringOptions("\nSource .dst file: ")
        {
            AllowSpaces = true,
        });
        if (dstRes.Status != PromptStatus.OK) return;
        string dstPath = dstRes.StringResult.Trim('"');

        var outRes = ed.GetString(new PromptStringOptions("\nJSON output path: ")
        {
            AllowSpaces = true,
        });
        if (outRes.Status != PromptStatus.OK) return;
        string outPath = outRes.StringResult.Trim('"');

        string json;
        try
        {
            json = ReadSheetSetJson(dstPath);
        }
        catch (Exception ex)
        {
            json = JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["ok"] = false,
                ["error"] = ex.Message,
                ["category"] = CategoryFor(ex),
            });
        }

        try
        {
            // Atomic write: temp file + rename so a partial write never
            // produces a half-formed JSON file the Python side has to handle.
            string tmp = outPath + ".tmp";
            File.WriteAllText(tmp, json);
            if (File.Exists(outPath)) File.Delete(outPath);
            File.Move(tmp, outPath);
            ed.WriteMessage($"\nMCP: sheet set written to {outPath}");
        }
        catch (Exception writeEx)
        {
            ed.WriteMessage($"\nMCP: failed to write {outPath}: {writeEx.Message}");
        }
    }

    /// <summary>
    /// Read the .dst at <paramref name="dstPath"/> using AcSmSheetSetMgr
    /// and return the serialized JSON payload. Visible for in-process
    /// integration tests.
    /// </summary>
    public static string ReadSheetSetJson(string dstPath)
    {
        if (string.IsNullOrWhiteSpace(dstPath))
            throw new ArgumentException("dstPath is empty", nameof(dstPath));
        if (!File.Exists(dstPath))
            throw new FileNotFoundException("File not found", dstPath);

        AcSmSheetSetMgr? mgr = null;
        AcSmDatabase? db = null;
        try
        {
            mgr = new AcSmSheetSetMgrClass();
            db = mgr.OpenDatabase(dstPath, /*bFailIfAlreadyOpen*/ true);
            if (db == null)
                throw new InvalidOperationException("OpenDatabase returned null");

            AcSmSheetSet ss = db.GetSheetSet();
            string ssName = db.GetName() ?? "";

            var sheets = new List<Dictionary<string, string>>();
            CollectSheets(ss, sheets);

            return JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["ok"] = true,
                ["name"] = ssName,
                ["sheets"] = sheets,
            });
        }
        finally
        {
            try { if (db != null && mgr != null) mgr.Close(db); } catch { }
            if (mgr != null) Marshal.FinalReleaseComObject(mgr);
        }
    }

    private static void CollectSheets(
        AcSmSheetSet ss, List<Dictionary<string, string>> sheets)
    {
        IAcSmEnumComponent enumr = ss.GetSheetEnumerator();
        IAcSmComponent? comp;
        while ((comp = enumr.Next()) != null)
        {
            switch (comp)
            {
                case AcSmSheet sheet:
                    sheets.Add(BuildSheetEntry(sheet));
                    break;
                case AcSmSubset subset:
                    // Subset folders contain more sheets — recurse so the
                    // output is flat regardless of how the user nested
                    // their .dst in Sheet Set Manager.
                    CollectSubset(subset, sheets);
                    break;
                // Unknown component type — ignore. Sheet sets can hold
                // resource refs and views that aren't sheets.
            }
        }
    }

    private static void CollectSubset(
        AcSmSubset subset, List<Dictionary<string, string>> sheets)
    {
        IAcSmEnumComponent enumr = subset.GetSheetEnumerator();
        IAcSmComponent? comp;
        while ((comp = enumr.Next()) != null)
        {
            switch (comp)
            {
                case AcSmSheet sheet:
                    sheets.Add(BuildSheetEntry(sheet));
                    break;
                case AcSmSubset child:
                    CollectSubset(child, sheets);
                    break;
            }
        }
    }

    private static Dictionary<string, string> BuildSheetEntry(AcSmSheet sheet)
    {
        string dwgFile = "";
        string layoutName = "";
        try
        {
            AcSmAcDbLayoutReference layoutRef = sheet.GetLayout();
            if (layoutRef != null)
            {
                // ResolveFileName turns relative paths into absolute, which
                // matches what the Python XML reader does for the legacy
                // format. Fall back to GetFileName if resolution fails.
                try { dwgFile = layoutRef.ResolveFileName() ?? ""; }
                catch { /* fall through */ }
                if (string.IsNullOrEmpty(dwgFile))
                    dwgFile = layoutRef.GetFileName() ?? "";
                layoutName = layoutRef.GetName() ?? "";
            }
        }
        catch { /* leave dwg/layout blank — better than a broken row */ }

        return new Dictionary<string, string>
        {
            ["number"]      = sheet.GetNumber() ?? "",
            ["title"]       = sheet.GetTitle() ?? "",
            ["description"] = sheet.GetDesc() ?? "",
            ["dwg_file"]    = dwgFile,
            ["layout"]      = layoutName,
        };
    }

    private static string CategoryFor(Exception ex) => ex switch
    {
        FileNotFoundException => "not_found",
        ArgumentException     => "invalid_arg",
        COMException          => "internal_error",
        _                      => "internal_error",
    };
}
