;;; mcp_reactors.lsp — VLR-based event source for the autocad-mcp server.
;;;
;;; Loads on top of mcp_dispatch.lsp (it reuses `*mcp-ipc-dir*`). When
;;; attached, AutoCAD writes a JSON-line per event (entity add/modify/erase,
;;; command start/end) to `<ipc-dir>/autocad_mcp_events.log`. The Python side
;;; (`src/autocad_mcp/events.py::EventTail`) tails that file.
;;;
;;; Compatibility: AutoCAD LT 2024+ for Windows (vl-* utilities supported;
;;; vlr-* reactor functions are part of that core LISP surface). Full AutoCAD
;;; supports the same surface.
;;;
;;; Usage:
;;;   (load "mcp_reactors.lsp")
;;;   (c:mcp-attach-reactors)   ; start recording
;;;   ... do drawing work ...
;;;   (c:mcp-detach-reactors)   ; stop

(vl-load-com)

;; Default IPC dir — overridden by mcp_dispatch.lsp if loaded. When loaded
;; standalone, mirror the Python default: <TEMP>/autocad-mcp/ (or the env
;; var when present), trailing slash required by callers.
(if (not *mcp-ipc-dir*)
  (progn
    (cond
      ((and (getenv "AUTOCAD_MCP_IPC_DIR")
            (> (strlen (getenv "AUTOCAD_MCP_IPC_DIR")) 0))
       (setq *mcp-ipc-dir* (getenv "AUTOCAD_MCP_IPC_DIR")))
      ((getenv "TEMP")
       (setq *mcp-ipc-dir* (strcat (getenv "TEMP") "/autocad-mcp")))
      (t
       (setq *mcp-ipc-dir* "C:/temp")))
    (setq *mcp-ipc-dir* (vl-string-translate "\\" "/" *mcp-ipc-dir*))
    (if (/= (substr *mcp-ipc-dir* (strlen *mcp-ipc-dir*)) "/")
      (setq *mcp-ipc-dir* (strcat *mcp-ipc-dir* "/")))
  )
)

(setq *mcp-events-path*
  (strcat *mcp-ipc-dir* "autocad_mcp_events.log"))

;; -----------------------------------------------------------------------
;; Event log writer
;; -----------------------------------------------------------------------

(defun mcp-events-escape (s / result i ch code)
  "Escape a string for inclusion as a JSON string value.
   Handles \" \\ \\n \\r \\t and any byte < 0x20 (control chars) as \\u00XX.
   Without this, an entity layer or command name containing a newline (legal
   but rare) would emit invalid JSON and the Python reader silently drops it."
  (if (null s) (setq s ""))
  (setq result "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (setq code (ascii ch))
    (cond
      ((= ch "\"") (setq result (strcat result "\\\"")))
      ((= ch "\\") (setq result (strcat result "\\\\")))
      ((= code 10) (setq result (strcat result "\\n")))
      ((= code 13) (setq result (strcat result "\\r")))
      ((= code 9)  (setq result (strcat result "\\t")))
      ((= code 8)  (setq result (strcat result "\\b")))
      ((= code 12) (setq result (strcat result "\\f")))
      ((< code 32)
       ;; Other control chars: \u00XX hex. itoa is decimal so we hand-roll
       ;; the two-digit hex for codes 0..31 (no need for general hex).
       (setq result (strcat result "\\u00"
                            (substr "0123456789abcdef" (1+ (/ code 16)) 1)
                            (substr "0123456789abcdef" (1+ (rem code 16)) 1))))
      (t (setq result (strcat result ch))))
    (setq i (1+ i)))
  result)

(defun mcp-events-write-line (line / fp)
  "Append `line` as one record to the event log. No-op on open failure."
  (setq fp (open *mcp-events-path* "a"))
  (if fp
    (progn
      (write-line line fp)
      (close fp))
  )
)

(defun mcp-local-epoch ( / cdate y mo d h mi s days-by-month leap-years yr days)
  "Return current time as seconds since 1970-01-01 *in local time*.

   AutoCAD's `CDATE` sysvar is a structured float YYYYMMDD.HHMMSS — much
   safer than `DATE` (which has shifted between Excel-epoch days, MJD, and
   full Julian Day Number across releases — AutoCAD 2027 returns the full
   JDN, ~2,461,000 today, breaking any formula that assumed a smaller
   base). CDATE is constant across versions.

   The result is local-time seconds since 1970-01-01 — NOT UTC, because
   AutoLISP has no UTC accessor in the core surface. Downstream consumers
   should treat the value as a local epoch (the field is named `ts_local`
   for clarity) or apply their own timezone offset."
  (setq cdate (getvar "CDATE"))
  ;; Integer part: YYYYMMDD
  (setq y  (fix (/ cdate 10000.0))
        mo (fix (/ (rem cdate 10000.0) 100.0))
        d  (fix (rem cdate 100.0)))
  ;; Fractional part: HHMMSS scaled by 10^6.
  (setq frac (- cdate (fix cdate)))
  (setq hms  (* frac 1000000.0))  ; HHMMSS as a number
  (setq h    (fix (/ hms 10000.0))
        mi   (fix (/ (rem hms 10000.0) 100.0))
        s    (rem hms 100.0))
  ;; Days at the start of each month in a non-leap year (Jan→Dec).
  (setq days-by-month '(0 31 59 90 120 151 181 212 243 273 304 334))
  ;; Count leap years between 1970 and y-1 inclusive.
  (setq leap-years 0 yr 1970)
  (while (< yr y)
    (if (or (= 0 (rem yr 400))
            (and (= 0 (rem yr 4)) (/= 0 (rem yr 100))))
      (setq leap-years (1+ leap-years)))
    (setq yr (1+ yr)))
  ;; Days from 1970-01-01 to YYYY-01-01 = (y-1970)*365 + leap-years.
  (setq days (+ (* (- y 1970) 365) leap-years))
  ;; Add days to start of month.
  (setq days (+ days (nth (1- mo) days-by-month)))
  ;; If current year is a leap year AND month > Feb, add 1.
  (if (and (> mo 2)
           (or (= 0 (rem y 400))
               (and (= 0 (rem y 4)) (/= 0 (rem y 100)))))
    (setq days (1+ days)))
  ;; Add day of month (1-indexed, so subtract 1).
  (setq days (+ days (1- d)))
  ;; Seconds = days*86400 + h*3600 + mi*60 + s.
  (+ (* days 86400.0) (* h 3600.0) (* mi 60.0) s)
)

(defun mcp-event-emit (kind handle etype layer extras / parts)
  "Append a JSON-line for one event. `extras` is a string of extra
   comma-prefixed key:value pairs (may be empty).
   `ts_local` is seconds-since-1970 in the user's local TZ — AutoCAD's
   DATE has no UTC accessor. Old field `ts` is retained for backward compat."
  (setq parts (strcat
    "{\"ts\":"        (rtos (mcp-local-epoch) 2 3)
    ",\"ts_local\":"  (rtos (mcp-local-epoch) 2 3)
    ",\"kind\":\""    (mcp-events-escape kind)
    "\",\"handle\":\"" (mcp-events-escape (if handle handle ""))
    "\",\"entity_type\":\"" (mcp-events-escape (if etype etype ""))
    "\",\"layer\":\""  (mcp-events-escape (if layer layer ""))
    "\""              extras
    "}"))
  (mcp-events-write-line parts)
)

;; -----------------------------------------------------------------------
;; Reactor callbacks
;; -----------------------------------------------------------------------

(defun mcp-rx-object-appended (reactor obj-list / ename ent-data etype handle layer)
  (foreach ename obj-list
    (setq ent-data (entget ename))
    (if ent-data
      (progn
        (setq etype  (cdr (assoc 0 ent-data))
              handle (cdr (assoc 5 ent-data))
              layer  (cdr (assoc 8 ent-data)))
        (mcp-event-emit "object_appended" handle etype layer "")
      )
    )
  )
)

(defun mcp-rx-object-modified (reactor obj-list / ename ent-data etype handle layer)
  (foreach ename obj-list
    (setq ent-data (entget ename))
    (if ent-data
      (progn
        (setq etype  (cdr (assoc 0 ent-data))
              handle (cdr (assoc 5 ent-data))
              layer  (cdr (assoc 8 ent-data)))
        (mcp-event-emit "object_modified" handle etype layer "")
      )
    )
  )
)

(defun mcp-rx-object-erased (reactor obj-list / ename ent-data etype handle layer)
  (foreach ename obj-list
    (setq ent-data (entget ename))
    (if ent-data
      (progn
        (setq etype  (cdr (assoc 0 ent-data))
              handle (cdr (assoc 5 ent-data))
              layer  (cdr (assoc 8 ent-data)))
        (mcp-event-emit "object_erased" handle etype layer "")
      )
      ;; If entget fails the entity may already be gone — still record what we can.
      (mcp-event-emit "object_erased" "" "" "" "")
    )
  )
)

(defun mcp-rx-command-will-start (reactor data / cmd)
  (setq cmd (car data))
  (mcp-event-emit "command_will_start" "" "" "" (strcat ",\"command\":\"" (mcp-events-escape cmd) "\""))
)

;; Last-seen entity handle (as an integer) — used to detect new entities at
;; command_ended time. Compensates for the fact that on AutoCAD 2024+ the
;; legacy `vlr-acdb-reactor :vlr-objectappended` callback is partially
;; deprecated and doesn't fire reliably; scanning forward from the last
;; seen handle at each command boundary gives us object-appended coverage
;; that approximates what the AcDb reactor used to provide for
;; command-driven entity creation.
(setq *mcp-last-handle-int* 0)

(defun mcp-hex-to-int (h / r i ch d)
  "Convert a hex handle string to a (non-negative) integer."
  (setq r 0 i 1)
  (while (<= i (strlen h))
    (setq ch (strcase (substr h i 1)))
    (cond
      ((and (>= (ascii ch) 48) (<= (ascii ch) 57))
       (setq d (- (ascii ch) 48)))
      ((and (>= (ascii ch) 65) (<= (ascii ch) 70))
       (setq d (- (ascii ch) 55)))
      (t (setq d 0)))
    (setq r (+ (* r 16) d))
    (setq i (1+ i)))
  r)

(defun mcp-scan-new-entities ( / ename data h handle-int)
  "Walk modelspace forward and emit `object_appended` for handles whose
   numeric value exceeds the highest one seen previously. Returns the
   number of new events emitted.

   Why: the AcDb reactor doesn't fire on AutoCAD 2024+. Handles in AutoCAD
   are monotonically increasing hex strings; comparing as integers gives
   a cheap O(n) way to spot freshly-appended entities."
  (setq count 0)
  (setq ename (entnext))
  (while ename
    (setq data (entget ename))
    (if data
      (progn
        (setq h (cdr (assoc 5 data)))
        (setq handle-int (mcp-hex-to-int h))
        (if (> handle-int *mcp-last-handle-int*)
          (progn
            (setq *mcp-last-handle-int* handle-int)
            (mcp-event-emit "object_appended"
                            h
                            (cdr (assoc 0 data))
                            (cdr (assoc 8 data))
                            "")
            (setq count (1+ count))))))
    (setq ename (entnext ename)))
  count)

(defun mcp-rx-command-ended (reactor data / cmd)
  (setq cmd (car data))
  (mcp-event-emit "command_ended" "" "" "" (strcat ",\"command\":\"" (mcp-events-escape cmd) "\""))
  ;; Scan for new entities created by this command.
  (mcp-scan-new-entities)
)

;; -----------------------------------------------------------------------
;; Attach / detach
;; -----------------------------------------------------------------------

(setq *mcp-reactors* nil)

(defun c:mcp-attach-reactors ( / ename data h handle-int)
  "Attach VLR reactors so user actions stream to the event log.
   Idempotent — re-attaching detaches first.

   The AcDb reactor handles the ideal case (direct entity-database events).
   On AutoCAD 2024+ those events fire unreliably, so the editor-reactor's
   command_ended callback also scans for new entities and emits
   `object_appended` for any handle past the last-seen one. The two
   mechanisms together give us redundant coverage."
  (if *mcp-reactors* (c:mcp-detach-reactors))
  ;; Snapshot the highest existing handle so the first command_ended scan
  ;; doesn't re-emit every entity that was there before attach.
  (setq *mcp-last-handle-int* 0)
  (setq ename (entnext))
  (while ename
    (setq data (entget ename))
    (if data
      (progn
        (setq h (cdr (assoc 5 data)))
        (setq handle-int (mcp-hex-to-int h))
        (if (> handle-int *mcp-last-handle-int*)
          (setq *mcp-last-handle-int* handle-int))))
    (setq ename (entnext ename)))
  (setq *mcp-reactors* (list
    (vlr-acdb-reactor nil '(
      (:vlr-objectappended . mcp-rx-object-appended)
      (:vlr-objectmodified . mcp-rx-object-modified)
      (:vlr-objecterased   . mcp-rx-object-erased)
    ))
    (vlr-editor-reactor nil '(
      (:vlr-commandwillstart . mcp-rx-command-will-start)
      (:vlr-commandended     . mcp-rx-command-ended)
    ))
  ))
  (mcp-event-emit "session_started" "" "" "" "")
  (princ (strcat "\nMCP reactors attached — writing to " *mcp-events-path*))
  (princ)
)

(defun c:mcp-detach-reactors ( / r)
  "Detach all MCP reactors."
  (if *mcp-reactors*
    (progn
      (foreach r *mcp-reactors*
        (if (vlr-added-p r) (vlr-remove r))
      )
      (setq *mcp-reactors* nil)
      (mcp-event-emit "session_ended" "" "" "" "")
      (princ "\nMCP reactors detached")
    )
    (princ "\nMCP reactors not attached")
  )
  (princ)
)

(princ "\n=== MCP Reactors v3.2 loaded ===")
(princ "\nEvent log: ")
(princ *mcp-events-path*)
(princ "\nRun (c:mcp-attach-reactors) to start recording")
(princ)
