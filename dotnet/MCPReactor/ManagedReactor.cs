// ManagedReactor.cs — subscribe to AutoCAD's managed events, append JSON
// lines to the event log on every relevant change.
//
// Subscribed events:
//   Database.ObjectAppended  → kind="object_appended"
//   Database.ObjectModified  → kind="object_modified"
//   Database.ObjectErased    → kind="object_erased" (only on Erased=true)
//   Document.CommandWillStart → kind="command_will_start"
//   Document.CommandEnded    → kind="command_ended"
//   Document.CommandCancelled → kind="command_cancelled"
//   Editor.SelectionChanged  → kind="selection_changed"  (new vs. LISP)
//
// Threading: callbacks fire on AutoCAD's UI thread. Writing to a file
// from that thread is acceptable as long as the write is fast — a
// JSON-line is ~150 bytes, the append-mode StreamWriter flush is
// microseconds. A static lock guards the writer against the rare case
// where two events for different documents race.

using System;
using System.Globalization;
using System.IO;
using System.Text;

using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;

namespace AutocadMcp.Reactor;

public sealed class ManagedReactor
{
    private static readonly object _writeLock = new();

    private readonly Editor _ed;
    private readonly StringBuilder _scratch = new(256);

    private Document? _doc;
    private Database? _db;
    private int _eventsWritten;

    public string LogPath { get; }
    public bool Attached => _doc is not null;
    public int EventsWritten => _eventsWritten;

    public ManagedReactor(string logPath, Editor ed)
    {
        LogPath = logPath;
        _ed = ed;
    }

    public void Attach(Document doc)
    {
        if (Attached) Detach();
        _doc = doc;
        _db = doc.Database;

        // Database events — these are the workhorse of "Claude watches my draw".
        _db.ObjectAppended += OnObjectAppended;
        _db.ObjectModified += OnObjectModified;
        _db.ObjectErased += OnObjectErased;

        // Document events — command lifecycle + selection.
        _doc.CommandWillStart += OnCommandWillStart;
        _doc.CommandEnded += OnCommandEnded;
        _doc.CommandCancelled += OnCommandCancelled;
        // Selection change is exposed via Document.ImpliedSelectionChanged.
        // This is the bit of surface the LISP reactor couldn't reach —
        // pickfirst-set mutations don't go through a command boundary.
        _doc.ImpliedSelectionChanged += OnSelectionChanged;

        Emit("session_started", null, null, null, null);
    }

    public void Detach()
    {
        if (_db is not null)
        {
            _db.ObjectAppended -= OnObjectAppended;
            _db.ObjectModified -= OnObjectModified;
            _db.ObjectErased -= OnObjectErased;
        }
        if (_doc is not null)
        {
            _doc.CommandWillStart -= OnCommandWillStart;
            _doc.CommandEnded -= OnCommandEnded;
            _doc.CommandCancelled -= OnCommandCancelled;
            _doc.ImpliedSelectionChanged -= OnSelectionChanged;
        }

        Emit("session_ended", null, null, null, null);

        _doc = null;
        _db = null;
    }

    // ----------- Database event handlers -----------

    private void OnObjectAppended(object? sender, ObjectEventArgs e)
        => EmitEntity("object_appended", e.DBObject);

    private void OnObjectModified(object? sender, ObjectEventArgs e)
        => EmitEntity("object_modified", e.DBObject);

    private void OnObjectErased(object? sender, ObjectErasedEventArgs e)
    {
        // Fires on both erase and unerase. We only record on actual erase
        // — unerase is rare and would double-count when the user undoes.
        if (!e.Erased) return;
        EmitEntity("object_erased", e.DBObject);
    }

    // ----------- Document / Editor handlers -----------

    private void OnCommandWillStart(object? sender, CommandEventArgs e)
        => Emit("command_will_start", null, null, null, $",\"command\":\"{Escape(e.GlobalCommandName)}\"");

    private void OnCommandEnded(object? sender, CommandEventArgs e)
        => Emit("command_ended", null, null, null, $",\"command\":\"{Escape(e.GlobalCommandName)}\"");

    private void OnCommandCancelled(object? sender, CommandEventArgs e)
        => Emit("command_cancelled", null, null, null, $",\"command\":\"{Escape(e.GlobalCommandName)}\"");

    private void OnSelectionChanged(object? sender, EventArgs e)
    {
        // PickfirstSelectionSet returns the current selection — count
        // and a small handle preview so a downstream consumer can decide
        // whether to ask for details. Pickfirst sets evaporate when the
        // user clicks elsewhere; reading it here is cheap and safe.
        if (_doc is null) return;
        try
        {
            PromptSelectionResult sel = _doc.Editor.SelectImplied();
            int count = 0;
            string firstHandle = "";
            if (sel.Status == PromptStatus.OK && sel.Value is not null)
            {
                count = sel.Value.Count;
                if (count > 0)
                {
                    var first = sel.Value[0];
                    firstHandle = first.ObjectId.Handle.ToString();
                }
            }
            Emit("selection_changed", null, null, null,
                 $",\"count\":{count},\"first_handle\":\"{Escape(firstHandle)}\"");
        }
        catch
        {
            // Selection inspection can fail mid-edit; swallow rather
            // than crash AutoCAD.
        }
    }

    // ----------- Output helpers -----------

    private void EmitEntity(string kind, DBObject obj)
    {
        // Reading from an entity outside a transaction is allowed for the
        // properties we touch (ObjectId, Handle, ClassName). The risky
        // bits (entity-data extraction) we skip.
        string handle = "";
        string entityType = "";
        string layer = "";
        try
        {
            handle = obj.ObjectId.Handle.ToString();
            entityType = obj.GetRXClass()?.DxfName ?? obj.GetType().Name;
            // Layer is only meaningful on Entity-derived objects.
            if (obj is Entity ent)
            {
                layer = ent.Layer ?? "";
            }
        }
        catch
        {
            // Object may have been erased between the event firing and
            // our read — surface whatever we have.
        }
        Emit(kind, handle, entityType, layer, null);
    }

    private void Emit(string kind, string? handle, string? entityType, string? layer, string? extras)
    {
        // Match the LISP reactor's record schema: ts, ts_local, kind,
        // handle, entity_type, layer, plus any extras (command, count, ...).
        double now = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        double nowLocal = (DateTime.Now - new DateTime(1970, 1, 1)).TotalSeconds;

        _scratch.Clear();
        _scratch.Append('{');
        _scratch.Append("\"ts\":").Append(now.ToString("F3", CultureInfo.InvariantCulture));
        _scratch.Append(",\"ts_local\":").Append(nowLocal.ToString("F3", CultureInfo.InvariantCulture));
        _scratch.Append(",\"kind\":\"").Append(Escape(kind)).Append('"');
        _scratch.Append(",\"handle\":\"").Append(Escape(handle ?? "")).Append('"');
        _scratch.Append(",\"entity_type\":\"").Append(Escape(entityType ?? "")).Append('"');
        _scratch.Append(",\"layer\":\"").Append(Escape(layer ?? "")).Append('"');
        if (extras is not null) _scratch.Append(extras);
        _scratch.Append('}');

        string line = _scratch.ToString();
        // Acquire the static lock so two reactors against two docs don't
        // interleave bytes mid-line.
        lock (_writeLock)
        {
            try
            {
                using var sw = new StreamWriter(LogPath, append: true, encoding: new UTF8Encoding(false));
                sw.WriteLine(line);
            }
            catch
            {
                // Best-effort — never let an IO failure crash AutoCAD.
            }
        }
        System.Threading.Interlocked.Increment(ref _eventsWritten);
    }

    /// <summary>
    /// Minimal JSON escape — quote, backslash, and any byte under 0x20
    /// emitted as \u00XX. Matches `mcp-events-escape` in the LISP reactor.
    /// </summary>
    private static string Escape(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        StringBuilder sb = new(s.Length + 8);
        foreach (char ch in s)
        {
            switch (ch)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                default:
                    if (ch < 0x20)
                    {
                        sb.Append("\\u").Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        sb.Append(ch);
                    }
                    break;
            }
        }
        return sb.ToString();
    }
}
