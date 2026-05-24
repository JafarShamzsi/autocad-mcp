;;; mcp_dispatch.lsp — File-based IPC dispatcher for AutoCAD MCP v3.2
;;;
;;; Protocol:
;;;   1. Python writes command JSON to <ipc-dir>/autocad_mcp_cmd_{id}.json
;;;   2. Python types "(c:mcp-dispatch)" + Enter
;;;   3. This function reads cmd, dispatches via command map, writes result JSON
;;;   4. Python polls for <ipc-dir>/autocad_mcp_result_{id}.json
;;;
;;; IPC dir: reads AUTOCAD_MCP_IPC_DIR env var when set; otherwise falls
;;; back to <TEMP>/autocad-mcp/ (TEMP from getenv, finally C:/temp/).
;;;
;;; SECURITY: No raw eval — dispatcher uses a command whitelist/map.
;;; Compatible with AutoCAD LT 2024+.

;; Load dependencies
(if (not report-error)
  (defun report-error (msg) (princ (strcat "\nERROR: " msg)))
)

;; IPC directory — prefer the explicit env var; otherwise <TEMP>/autocad-mcp/.
;; The trailing slash is required by callers that do `strcat *mcp-ipc-dir* file`.
(defun mcp-resolve-ipc-dir ( / env-val temp-val raw)
  (setq env-val (getenv "AUTOCAD_MCP_IPC_DIR"))
  (cond
    ((and env-val (> (strlen env-val) 0))
     (setq raw env-val))
    ((setq temp-val (getenv "TEMP"))
     (setq raw (strcat temp-val "/autocad-mcp")))
    (t
     (setq raw "C:/temp"))
  )
  ;; Normalize backslashes and ensure a trailing slash.
  (setq raw (vl-string-translate "\\" "/" raw))
  (if (/= (substr raw (strlen raw)) "/")
    (setq raw (strcat raw "/"))
  )
  raw
)

(setq *mcp-ipc-dir* (mcp-resolve-ipc-dir))

;; -----------------------------------------------------------------------
;; JSON-like output helpers (minimal, no external library)
;; -----------------------------------------------------------------------

(defun mcp-write-result (filepath request-id ok-flag payload error-msg / fp)
  "Write a result JSON file. Atomic: write to .tmp then rename."
  (setq tmp-path (strcat filepath ".tmp"))
  (setq fp (open tmp-path "w"))
  (if fp
    (progn
      (write-line "{" fp)
      (write-line (strcat "  \"request_id\": \"" request-id "\",") fp)
      (if ok-flag
        (progn
          (write-line "  \"ok\": true," fp)
          (write-line (strcat "  \"payload\": " payload) fp)
        )
        (progn
          (write-line "  \"ok\": false," fp)
          (write-line (strcat "  \"error\": \"" (mcp-escape-string error-msg) "\"") fp)
        )
      )
      (write-line "}" fp)
      (close fp)
      ;; Rename .tmp to final path (atomic on NTFS)
      (vl-file-rename tmp-path filepath)
    )
    (princ (strcat "\nMCP: Cannot open result file: " tmp-path))
  )
)

(defun mcp-escape-string (s / result i ch)
  "Escape quotes and backslashes in a string for JSON."
  (if (null s) (setq s ""))
  (setq result "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (cond
      ((= ch "\"") (setq result (strcat result "\\\"")))
      ((= ch "\\") (setq result (strcat result "\\\\")))
      (t (setq result (strcat result ch)))
    )
    (setq i (1+ i))
  )
  result
)

(defun mcp-read-file-lines (filepath / fp line lines)
  "Read all lines from a file into a single string."
  (setq fp (open filepath "r"))
  (if (not fp) (progn (princ (strcat "\nMCP: Cannot read: " filepath)) nil)
    (progn
      (setq lines "")
      (while (setq line (read-line fp))
        (setq lines (strcat lines line))
      )
      (close fp)
      lines
    )
  )
)

;; -----------------------------------------------------------------------
;; Simple JSON parser (extracts string values by key)
;; -----------------------------------------------------------------------

(defun mcp-json-unescape (raw / out i len ch nxt code hex)
  "Decode JSON escape sequences in RAW. Handles \\\" \\\\ \\/ \\n \\r \\t \\b
   \\f and \\uXXXX. Unknown escapes are passed through verbatim so a malformed
   input doesn't crash the dispatcher.

   `(read-line ...)` strips real newlines on the way in, but the Python
   side encodes user text with the standard JSON escapes — without this
   pass the LISP layer used to deliver text containing visible `\\\"`,
   `\\n`, etc. directly to AutoCAD."
  (setq out "" i 1 len (strlen raw))
  (while (<= i len)
    (setq ch (substr raw i 1))
    (cond
      ((and (= ch "\\") (< i len))
       (setq nxt (substr raw (1+ i) 1))
       (cond
         ((= nxt "\"") (setq out (strcat out "\""))   (setq i (+ i 2)))
         ((= nxt "\\") (setq out (strcat out "\\"))   (setq i (+ i 2)))
         ((= nxt "/")  (setq out (strcat out "/"))    (setq i (+ i 2)))
         ((= nxt "n")  (setq out (strcat out "\n"))   (setq i (+ i 2)))
         ((= nxt "r")  (setq out (strcat out "\r"))   (setq i (+ i 2)))
         ((= nxt "t")  (setq out (strcat out "\t"))   (setq i (+ i 2)))
         ((= nxt "b")  (setq out (strcat out (chr 8)))  (setq i (+ i 2)))
         ((= nxt "f")  (setq out (strcat out (chr 12))) (setq i (+ i 2)))
         ((and (= nxt "u") (<= (+ i 5) len))
          ;; \uXXXX — clamp to chr 32..126 if out of representable range
          ;; so AutoCAD doesn't choke on a non-printable. The Python side
          ;; only emits this form for code points >= 0x80, which on Windows
          ;; AutoCAD render as glyphs anyway if the active code page covers
          ;; them; we don't try to reconstruct UTF-8 bytes here.
          (setq hex (substr raw (+ i 2) 4))
          (setq code (mcp-parse-hex4 hex))
          (if (and code (> code 0) (< code 256))
            (setq out (strcat out (chr code)))
            ;; Out-of-range or unparseable: keep the raw form so the caller
            ;; can see what came across.
            (setq out (strcat out "\\u" hex)))
          (setq i (+ i 6)))
         (t
          ;; Unknown escape: keep the backslash + char (safer than dropping).
          (setq out (strcat out "\\" nxt))
          (setq i (+ i 2)))))
      (t
       (setq out (strcat out ch))
       (setq i (1+ i)))))
  out)

(defun mcp-parse-hex4 (s / i ch v digit)
  "Parse a 4-char hex string into an integer, nil on bad input."
  (if (or (null s) (/= (strlen s) 4))
    nil
    (progn
      (setq v 0 i 1)
      (while (and v (<= i 4))
        (setq ch (substr s i 1))
        (setq digit
          (cond
            ((and (>= (ascii ch) 48) (<= (ascii ch) 57))
             (- (ascii ch) 48))
            ((and (>= (ascii ch) 65) (<= (ascii ch) 70))
             (+ 10 (- (ascii ch) 65)))
            ((and (>= (ascii ch) 97) (<= (ascii ch) 102))
             (+ 10 (- (ascii ch) 97)))
            (t nil)))
        (if digit
          (setq v (+ (* v 16) digit) i (1+ i))
          (setq v nil)))
      v)))

(defun mcp-json-get-string (json key / search-str pos end-pos raw)
  "Extract a string value for a given key from JSON text.

   Walks past escape sequences correctly, then unescapes them via
   `mcp-json-unescape` so values containing quotes, newlines, etc.
   round-trip cleanly from Python."
  (setq search-str (strcat "\"" key "\""))
  (setq pos (vl-string-search search-str json))
  (if (null pos) nil
    (progn
      ;; Find the colon after key
      (setq pos (vl-string-search ":" json pos))
      (if (null pos) nil
        (progn
          ;; Find opening quote of value
          (setq pos (vl-string-search "\"" json (1+ pos)))
          (if (null pos) nil
            (progn
              (setq pos (+ pos 2))  ; 0-based search result + 2 = 1-based position after quote
              ;; Find closing quote (skip escaped quotes)
              (setq end-pos pos)
              (while (and (<= end-pos (strlen json))
                          (or (= end-pos pos)
                              (/= (substr json end-pos 1) "\"")))
                ;; Handle escaped characters
                (if (= (substr json end-pos 1) "\\")
                  (setq end-pos (+ end-pos 2))
                  (setq end-pos (1+ end-pos))
                )
              )
              (setq raw (substr json pos (- end-pos pos)))
              (mcp-json-unescape raw))))))))

(defun mcp-json-get-number (json key / search-str pos num-start num-end ch
                                       digit-set num-str)
  "Extract a number value for a given key from JSON text.

   Accepts the JSON number grammar: optional leading minus, digits, optional
   `.digits`, optional `[eE][+-]?digits`. AutoLISP's `atof` parses scientific
   notation, so once we've correctly delimited the run of number-like
   characters it does the right thing."
  (setq search-str (strcat "\"" key "\""))
  (setq pos (vl-string-search search-str json))
  (if (null pos) nil
    (progn
      (setq pos (vl-string-search ":" json pos))
      (if (null pos) nil
        (progn
          (setq pos (+ pos 2))  ; 0-based search result + 2 = 1-based position after colon
          ;; Skip whitespace
          (while (and (<= pos (strlen json))
                      (member (substr json pos 1) '(" " "\t" "\n")))
            (setq pos (1+ pos))
          )
          ;; Accept the JSON number grammar conservatively. `e`/`E` are part
          ;; of scientific notation; the prior set didn't include them and
          ;; silently truncated `1.5e3` to `1.5`.
          (setq digit-set '("0" "1" "2" "3" "4" "5" "6" "7" "8" "9"
                            "." "-" "+" "e" "E"))
          (setq num-start pos num-end pos)
          (while (and (<= num-end (strlen json))
                      (member (substr json num-end 1) digit-set))
            (setq num-end (1+ num-end))
          )
          (setq num-str (substr json num-start (- num-end num-start)))
          ;; Empty or single-char (`-`, `.`, `+`) means we never saw a digit.
          ;; Return nil so callers can distinguish "missing" from "0".
          (if (or (= num-str "") (= num-str "-") (= num-str "+") (= num-str "."))
            nil
            (atof num-str)))))))

;; -----------------------------------------------------------------------
;; String splitting utility (used by semicolon-delimited encodings)
;; -----------------------------------------------------------------------

(defun mcp-split-string (str delim / pos result token)
  "Split a string by single-char delimiter. Returns a list of strings."
  (setq result '())
  (while (setq pos (vl-string-search delim str))
    (setq token (substr str 1 pos))
    (setq result (append result (list token)))
    (setq str (substr str (+ pos 2)))
  )
  (setq result (append result (list str)))
  result
)

;; -----------------------------------------------------------------------
;; Command dispatcher — WHITELIST ONLY, no eval
;; -----------------------------------------------------------------------

(defun mcp-dispatch-command (cmd-name params-json / result)
  "Dispatch a command by name. Returns (ok . payload-or-error)."
  (cond
    ;; --- Ping ---
    ((= cmd-name "ping")
     (cons T "\"pong\""))

    ;; --- Freehand LISP execution ---
    ((= cmd-name "execute-lisp")
     (mcp-cmd-execute-lisp params-json))

    ;; --- Undo / Redo ---
    ((= cmd-name "undo")
     (command "_.UNDO" "1") (cons T "\"undone\""))

    ((= cmd-name "redo")
     (command "_.REDO") (cons T "\"redone\""))

    ;; --- Drawing info ---
    ((= cmd-name "drawing-info")
     (mcp-cmd-drawing-info))

    ;; --- Layer operations ---
    ((= cmd-name "layer-list")
     (mcp-cmd-layer-list))

    ((= cmd-name "layer-create")
     (mcp-cmd-layer-create params-json))

    ((= cmd-name "layer-set-current")
     (mcp-cmd-layer-set-current params-json))

    ((= cmd-name "layer-set-properties")
     (mcp-cmd-layer-set-properties params-json))

    ((= cmd-name "layer-freeze")
     (mcp-cmd-layer-freeze params-json))

    ((= cmd-name "layer-thaw")
     (mcp-cmd-layer-thaw params-json))

    ((= cmd-name "layer-lock")
     (mcp-cmd-layer-lock params-json))

    ((= cmd-name "layer-unlock")
     (mcp-cmd-layer-unlock params-json))

    ;; --- Entity creation ---
    ((= cmd-name "create-line")
     (mcp-cmd-create-line params-json))

    ((= cmd-name "create-circle")
     (mcp-cmd-create-circle params-json))

    ((= cmd-name "create-polyline")
     (mcp-cmd-create-polyline params-json))

    ((= cmd-name "create-rectangle")
     (mcp-cmd-create-rectangle params-json))

    ((= cmd-name "create-text")
     (mcp-cmd-create-text params-json))

    ((= cmd-name "create-arc")
     (mcp-cmd-create-arc params-json))

    ((= cmd-name "create-ellipse")
     (mcp-cmd-create-ellipse params-json))

    ((= cmd-name "create-mtext")
     (mcp-cmd-create-mtext params-json))

    ((= cmd-name "create-hatch")
     (mcp-cmd-create-hatch params-json))

    ;; --- Entity queries ---
    ((= cmd-name "entity-count")
     (mcp-cmd-entity-count params-json))

    ((= cmd-name "entity-list")
     (mcp-cmd-entity-list params-json))

    ((= cmd-name "entity-get")
     (mcp-cmd-entity-get params-json))

    ((= cmd-name "entity-erase")
     (mcp-cmd-entity-erase params-json))

    ;; --- Entity modification ---
    ((= cmd-name "entity-move")
     (mcp-cmd-entity-move params-json))

    ((= cmd-name "entity-copy")
     (mcp-cmd-entity-copy params-json))

    ((= cmd-name "entity-rotate")
     (mcp-cmd-entity-rotate params-json))

    ((= cmd-name "entity-scale")
     (mcp-cmd-entity-scale params-json))

    ((= cmd-name "entity-mirror")
     (mcp-cmd-entity-mirror params-json))

    ((= cmd-name "entity-offset")
     (mcp-cmd-entity-offset params-json))

    ((= cmd-name "entity-array")
     (mcp-cmd-entity-array params-json))

    ((= cmd-name "entity-fillet")
     (mcp-cmd-entity-fillet params-json))

    ((= cmd-name "entity-chamfer")
     (mcp-cmd-entity-chamfer params-json))

    ;; --- View ---
    ((= cmd-name "zoom-extents")
     (command "_.ZOOM" "_E")
     (cons T "\"zoomed to extents\""))

    ((= cmd-name "zoom-window")
     (progn
       (setq x1 (mcp-json-get-number params-json "x1"))
       (setq y1 (mcp-json-get-number params-json "y1"))
       (setq x2 (mcp-json-get-number params-json "x2"))
       (setq y2 (mcp-json-get-number params-json "y2"))
       (command "_.ZOOM" "_W" (list x1 y1 0) (list x2 y2 0))
       (cons T "\"zoomed to window\"")))

    ;; --- Drawing file ops ---
    ((= cmd-name "drawing-save")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if (and path (> (strlen path) 0))
         (progn
           (setvar "FILEDIA" 0)
           (command "_.SAVEAS" "" path)
           (setvar "FILEDIA" 1)
           (cons T (strcat "\"saved to: " (mcp-escape-string path) "\"")))
         (progn (command "_.QSAVE") (cons T "\"saved\"")))))

    ((= cmd-name "drawing-save-as-dxf")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if path
         (progn (command "_.SAVEAS" "DXF" path) (cons T (strcat "\"" path "\"")))
         (cons nil "Save path required"))))

    ((= cmd-name "drawing-purge")
     (command "_.-PURGE" "_ALL" "*" "_N")
     (cons T "\"purged\""))

    ((= cmd-name "drawing-open")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if path
         (progn
           (setvar "FILEDIA" 0)
           (command "_.OPEN" path)
           (setvar "FILEDIA" 1)
           (cons T (strcat "\"opened: " (mcp-escape-string path) "\"")))
         (cons nil "Path required"))))

    ;; --- P&ID ---
    ((= cmd-name "pid-setup-layers")
     (if c:setup-pid-layers
       (progn (c:setup-pid-layers) (cons T "\"P&ID layers created\""))
       (cons nil "pid_tools.lsp not loaded")))

    ((= cmd-name "pid-insert-symbol")
     (mcp-cmd-pid-insert-symbol params-json))

    ((= cmd-name "pid-draw-process-line")
     (mcp-cmd-pid-draw-process-line params-json))

    ((= cmd-name "pid-connect-equipment")
     (mcp-cmd-pid-connect-equipment params-json))

    ((= cmd-name "pid-add-flow-arrow")
     (mcp-cmd-pid-add-flow-arrow params-json))

    ((= cmd-name "pid-add-equipment-tag")
     (mcp-cmd-pid-add-equipment-tag params-json))

    ((= cmd-name "pid-add-line-number")
     (mcp-cmd-pid-add-line-number params-json))

    ((= cmd-name "pid-insert-valve")
     (mcp-cmd-pid-insert-valve params-json))

    ((= cmd-name "pid-insert-instrument")
     (mcp-cmd-pid-insert-instrument params-json))

    ((= cmd-name "pid-insert-pump")
     (mcp-cmd-pid-insert-pump params-json))

    ((= cmd-name "pid-insert-tank")
     (mcp-cmd-pid-insert-tank params-json))

    ;; --- Block operations ---
    ((= cmd-name "block-list")
     (mcp-cmd-block-list))

    ((= cmd-name "block-insert")
     (mcp-cmd-block-insert params-json))

    ((= cmd-name "block-insert-with-attributes")
     (mcp-cmd-block-insert-with-attribs params-json))

    ((= cmd-name "block-get-attributes")
     (mcp-cmd-block-get-attributes params-json))

    ((= cmd-name "block-update-attribute")
     (mcp-cmd-block-update-attribute params-json))

    ((= cmd-name "block-define")
     (cons nil "block-define not available via IPC (use ezdxf backend)"))

    ((= cmd-name "library-define")
     (mcp-cmd-library-define params-json))

    ;; --- Annotation ---
    ((= cmd-name "create-dimension-linear")
     (mcp-cmd-create-dimension-linear params-json))

    ((= cmd-name "create-dimension-aligned")
     (mcp-cmd-create-dimension-aligned params-json))

    ((= cmd-name "create-dimension-angular")
     (mcp-cmd-create-dimension-angular params-json))

    ((= cmd-name "create-dimension-radius")
     (mcp-cmd-create-dimension-radius params-json))

    ((= cmd-name "create-leader")
     (mcp-cmd-create-leader params-json))

    ;; --- Architectural primitives (AIA/NCS) ---
    ((= cmd-name "arch-setup-layers")
     (mcp-cmd-arch-setup-layers))

    ((= cmd-name "arch-wall")
     (mcp-cmd-arch-wall params-json))

    ((= cmd-name "arch-wall-with-openings")
     (mcp-cmd-arch-wall-with-openings params-json))

    ((= cmd-name "arch-door")
     (mcp-cmd-arch-door params-json))

    ((= cmd-name "arch-window")
     (mcp-cmd-arch-window params-json))

    ((= cmd-name "arch-room")
     (mcp-cmd-arch-room params-json))

    ((= cmd-name "arch-dimension-chain")
     (mcp-cmd-arch-dimension-chain params-json))

    ((= cmd-name "arch-emit")
     (if *mcp-arch-tools-loaded*
       (mcp-cmd-arch-emit params-json)
       (cons nil "arch_tools.lsp not loaded. APPLOAD lisp-code/arch_tools.lsp or add lisp-code/ to Options → Files → Support File Search Path.")))

    ;; --- Drawing management ---
    ((= cmd-name "drawing-create")
     (mcp-cmd-drawing-create params-json))

    ((= cmd-name "drawing-get-variables")
     (mcp-cmd-drawing-get-variables params-json))

    ((= cmd-name "drawing-plot-pdf")
     (mcp-cmd-drawing-plot-pdf params-json))

    ;; --- P&ID list symbols ---
    ((= cmd-name "pid-list-symbols")
     (mcp-cmd-pid-list-symbols params-json))

    ;; --- Unknown ---
    (t (cons nil (strcat "Unknown command: " cmd-name)))
  )
)

;; -----------------------------------------------------------------------
;; Command implementations
;; -----------------------------------------------------------------------

(defun mcp-cmd-drawing-info ( / count layers layer-list)
  "Return drawing info: entity count, layers, extents."
  (setq count 0)
  (setq ent (entnext))
  (while ent
    (setq count (1+ count))
    (setq ent (entnext ent))
  )
  (setq layer-list "")
  (setq layers (tblnext "LAYER" T))
  (while layers
    (if (> (strlen layer-list) 0)
      (setq layer-list (strcat layer-list ",\"" (cdr (assoc 2 layers)) "\""))
      (setq layer-list (strcat "\"" (cdr (assoc 2 layers)) "\""))
    )
    (setq layers (tblnext "LAYER"))
  )
  (cons T (strcat "{\"entity_count\":" (itoa count) ",\"layers\":[" layer-list "]}"))
)

(defun mcp-cmd-layer-list ( / layers layer-list name)
  "Return all layers as JSON array."
  (setq layer-list "")
  (setq layers (tblnext "LAYER" T))
  (while layers
    (setq name (cdr (assoc 2 layers)))
    (if (> (strlen layer-list) 0)
      (setq layer-list (strcat layer-list ",{\"name\":\"" name "\",\"color\":" (itoa (cdr (assoc 62 layers))) "}"))
      (setq layer-list (strcat "{\"name\":\"" name "\",\"color\":" (itoa (cdr (assoc 62 layers))) "}"))
    )
    (setq layers (tblnext "LAYER"))
  )
  (cons T (strcat "{\"layers\":[" layer-list "]}"))
)

(defun mcp-cmd-layer-create (params / name color linetype)
  (setq name (mcp-json-get-string params "name"))
  (setq color (mcp-json-get-string params "color"))
  (setq linetype (mcp-json-get-string params "linetype"))
  (if (not color) (setq color "white"))
  (if (not linetype) (setq linetype "CONTINUOUS"))
  (ensure_layer_exists name color linetype)
  (cons T (strcat "{\"name\":\"" name "\"}"))
)

(defun mcp-cmd-layer-set-current (params / name)
  (setq name (mcp-json-get-string params "name"))
  (setvar "CLAYER" name)
  (cons T (strcat "{\"current_layer\":\"" name "\"}"))
)

(defun mcp-cmd-create-line (params / x1 y1 x2 y2 layer)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_LINE" (list x1 y1 0.0) (list x2 y2 0.0) "")
  (cons T (strcat "{\"entity_type\":\"LINE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-circle (params / cx cy radius layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_CIRCLE" (list cx cy 0.0) radius)
  (cons T (strcat "{\"entity_type\":\"CIRCLE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-polyline (params / pts-str closed layer pairs pt-str cx cy)
  (setq pts-str (mcp-json-get-string params "points_str"))
  (setq closed (mcp-json-get-string params "closed"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (command "_PLINE")
      (setq pairs (mcp-split-string pts-str ";"))
      (foreach pt-str pairs
        (setq cx (atof (car (mcp-split-string pt-str ","))))
        (setq cy (atof (cadr (mcp-split-string pt-str ","))))
        (command (list cx cy 0.0))
      )
      (if (= closed "1") (command "_C") (command ""))
      (cons T (strcat "{\"entity_type\":\"LWPOLYLINE\",\"handle\":\""
                      (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
  )
)

(defun mcp-cmd-create-rectangle (params / x1 y1 x2 y2 layer)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_RECTANG" (list x1 y1 0.0) (list x2 y2 0.0))
  (cons T (strcat "{\"entity_type\":\"LWPOLYLINE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-text (params / x y text height rotation layer)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq text (mcp-json-get-string params "text"))
  (setq height (mcp-json-get-number params "height"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not height) (setq height 2.5))
  (if (not rotation) (setq rotation 0.0))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_TEXT" "J" "M" (list x y 0.0) height rotation text)
  (cons T (strcat "{\"entity_type\":\"TEXT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-entity-count (params / layer count ent ent-data)
  (setq layer (mcp-json-get-string params "layer"))
  (setq count 0 ent (entnext))
  (while ent
    (setq ent-data (entget ent))
    (if (or (not layer) (= (cdr (assoc 8 ent-data)) layer))
      (setq count (1+ count))
    )
    (setq ent (entnext ent))
  )
  (cons T (strcat "{\"count\":" (itoa count) "}"))
)

(defun mcp-cmd-entity-list (params / layer limit-val limit-cap count truncated entities ent ent-data etype handle elayer)
  "List entities with an optional layer filter and a `limit` cap.
   When the cap is reached, set truncated=true so callers know to refine
   the filter — without this guard, listing a 100k-entity drawing produces
   unbounded JSON output."
  (setq layer (mcp-json-get-string params "layer"))
  (setq limit-val (mcp-json-get-number params "limit"))
  ;; nil → 1000 (default); 0 → unlimited.
  (setq limit-cap (cond ((null limit-val) 1000)
                        ((<= limit-val 0) nil)
                        (t (fix limit-val))))
  (setq entities "" count 0 truncated "false" ent (entnext))
  (while (and ent (or (null limit-cap) (< count limit-cap)))
    (setq ent-data (entget ent))
    (setq etype (cdr (assoc 0 ent-data)))
    (setq handle (cdr (assoc 5 ent-data)))
    (setq elayer (cdr (assoc 8 ent-data)))
    (if (or (not layer) (= elayer layer))
      (progn
        (if (> (strlen entities) 0)
          (setq entities (strcat entities ","))
        )
        (setq entities (strcat entities "{\"type\":\"" etype "\",\"handle\":\"" handle "\",\"layer\":\"" elayer "\"}"))
        (setq count (1+ count))
      )
    )
    (setq ent (entnext ent))
  )
  ;; If we hit the cap AND there's still another entity past it, mark truncated.
  (if (and limit-cap (= count limit-cap) ent)
    (setq truncated "true"))
  (cons T (strcat "{\"entities\":[" entities "],\"count\":" (itoa count) ",\"truncated\":" truncated "}"))
)

(defun mcp-cmd-entity-erase (params / entity-id ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last")
    (progn
      (setq ent (entlast))
      (if ent (progn (entdel ent) (cons T "\"erased last entity\""))
        (cons nil "No entity to erase")))
    (progn
      (setq ent (handent entity-id))
      (if ent (progn (entdel ent) (cons T (strcat "\"erased " entity-id "\"")))
        (cons nil (strcat "Entity not found: " entity-id))))
  )
)

(defun mcp-cmd-entity-move (params / entity-id dx dy ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq dx (mcp-json-get-number params "dx"))
  (setq dy (mcp-json-get-number params "dy"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if ent
    (progn
      (command "_.MOVE" ent "" '(0 0 0) (list dx dy 0))
      (cons T "\"moved\""))
    (cons nil "Entity not found")
  )
)

;; --- Freehand LISP execution ---

(defun mcp-cmd-execute-lisp (params / code-file result old-secureload restored)
  ;; Suppress AutoCAD's SECURELOAD dialog for the duration of the temp-file
  ;; load only. Wrapped so a failure inside `load` doesn't leak SECURELOAD=0
  ;; for the rest of the AutoCAD session.
  ;;
  ;; Security note: the Python side gates this command behind
  ;; AUTOCAD_MCP_ENABLE_LISP_EXEC; reaching the dispatcher means the user
  ;; has already opted into arbitrary AutoLISP execution. The SECURELOAD
  ;; toggle is a UX concession (no modal prompt per MCP call), not a new
  ;; trust boundary.
  (setq code-file (mcp-json-get-string params "code_file"))
  (if (not code-file)
    (cons nil "code_file parameter required")
    (if (not (findfile code-file))
      (cons nil (strcat "Code file not found: " code-file))
      (progn
        (setq old-secureload (getvar "SECURELOAD"))
        (setq restored nil)
        (setq result
          (vl-catch-all-apply
            '(lambda ()
               (setvar "SECURELOAD" 0)
               (load code-file))))
        ;; Restore even if `load` raised. Catch the restore too so a
        ;; getvar/setvar mismatch can't poison the result message.
        (vl-catch-all-apply
          '(lambda ()
             (if old-secureload (setvar "SECURELOAD" old-secureload))
             (setq restored T)))
        (cond
          ((vl-catch-all-error-p result)
           (cons nil
             (strcat "LISP error: "
                     (vl-catch-all-error-message result)
                     (if restored "" " [WARNING: SECURELOAD restore failed]"))))
          (t
           (cons T
             (strcat "\""
                     (mcp-escape-string (vl-princ-to-string result))
                     "\""))))))))

;; --- Drawing create implementation ---

(defun mcp-cmd-drawing-create (params / force ss dbmod)
  "Reset current drawing to a clean state (erase all, purge, reset to layer 0).
   Using _.NEW would create a new document tab with a fresh LISP namespace,
   breaking the IPC dispatcher. This approach preserves the dispatcher.

   Destructive against the currently-open drawing. Refuses to run if the
   drawing has unsaved changes (DBMOD != 0) unless data.force is set."
  (setq force (mcp-json-get-string params "force"))
  (setq dbmod (getvar "DBMOD"))
  (cond
    ((and (/= dbmod 0) (not (or (= force "1") (= force "true"))))
     (cons nil (strcat
       "Active drawing has unsaved changes (DBMOD=" (itoa dbmod) "). "
       "Save first or pass force=\"1\" to discard. drawing-create is "
       "destructive on the file_ipc backend: it erases every entity in the "
       "active drawing rather than opening a new tab (which would break the "
       "LISP dispatcher's namespace).")))
    (t
     (if (setq ss (ssget "_X"))
       (progn (command "_.ERASE" ss "") (setq ss nil)))
     (setvar "CLAYER" "0")
     (command "_.-PURGE" "_ALL" "*" "_N")
     (cons T (strcat "{\"drawing\":\"" (mcp-escape-string (getvar "DWGNAME")) "\"}")))
  )
)

;; --- P&ID command implementations ---

(defun mcp-cmd-pid-insert-symbol (params / category symbol x y scale rotation)
  (setq category (mcp-json-get-string params "category"))
  (setq symbol (mcp-json-get-string params "symbol"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-pid-block
    (progn
      (c:insert-pid-block category symbol x y scale rotation)
      (cons T (strcat "{\"symbol\":\"" symbol "\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-draw-process-line (params / x1 y1 x2 y2)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if c:draw-process-line
    (progn (c:draw-process-line x1 y1 x2 y2) (cons T "\"process line drawn\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-connect-equipment (params / x1 y1 x2 y2)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if c:connect-equipment
    (progn (c:connect-equipment x1 y1 x2 y2) (cons T "\"equipment connected\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-flow-arrow (params / x y rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:add-flow-arrow
    (progn (c:add-flow-arrow x y rotation) (cons T "\"flow arrow added\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-equipment-tag (params / x y tag description)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq tag (mcp-json-get-string params "tag"))
  (setq description (mcp-json-get-string params "description"))
  (if (not description) (setq description ""))
  (if c:add-equipment-tag
    (progn (c:add-equipment-tag x y tag description) (cons T (strcat "\"tagged: " tag "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-line-number (params / x y line-num spec)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq line-num (mcp-json-get-string params "line_num"))
  (setq spec (mcp-json-get-string params "spec"))
  (if c:add-line-number
    (progn (c:add-line-number x y line-num spec) (cons T (strcat "\"line number: " line-num "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-valve (params / x y valve-type rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq valve-type (mcp-json-get-string params "valve_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-valve-on-line
    (progn (c:insert-valve-on-line x y valve-type rotation) (cons T (strcat "\"valve: " valve-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-instrument (params / x y inst-type rotation tag-id range-value)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq inst-type (mcp-json-get-string params "instrument_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (setq tag-id (mcp-json-get-string params "tag_id"))
  (setq range-value (mcp-json-get-string params "range_value"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-instrument
    (progn
      (c:insert-instrument x y inst-type rotation)
      (if (and tag-id (> (strlen tag-id) 0))
        (c:insert-instrument-with-tag x y inst-type tag-id (if range-value range-value ""))
      )
      (cons T (strcat "\"instrument: " inst-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-pump (params / x y pump-type rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq pump-type (mcp-json-get-string params "pump_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-pump
    (progn (c:insert-pump x y pump-type rotation) (cons T (strcat "\"pump: " pump-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-tank (params / x y tank-type scale)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq tank-type (mcp-json-get-string params "tank_type"))
  (setq scale (mcp-json-get-number params "scale"))
  (if (not scale) (setq scale 1.0))
  (if c:insert-tank
    (progn (c:insert-tank x y tank-type scale) (cons T (strcat "\"tank: " tank-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

;; --- Additional entity creation ---

(defun mcp-cmd-create-arc (params / cx cy radius sa ea layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq sa (mcp-json-get-number params "start_angle"))
  (setq ea (mcp-json-get-number params "end_angle"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_ARC" "_C" (list cx cy 0.0) (list (+ cx radius) cy 0.0) "_A" (- ea sa))
  (cons T (strcat "{\"entity_type\":\"ARC\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-ellipse (params / cx cy mx my ratio layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq mx (mcp-json-get-number params "major_x"))
  (setq my (mcp-json-get-number params "major_y"))
  (setq ratio (mcp-json-get-number params "ratio"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_ELLIPSE" "_C" (list cx cy 0.0) (list mx my 0.0) ratio)
  (cons T (strcat "{\"entity_type\":\"ELLIPSE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-mtext (params / x y width text height layer)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq width (mcp-json-get-number params "width"))
  (setq text (mcp-json-get-string params "text"))
  (setq height (mcp-json-get-number params "height"))
  (if (not height) (setq height 2.5))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_MTEXT" (list x y 0.0) "_H" height "_W" width text "")
  (cons T (strcat "{\"entity_type\":\"MTEXT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-hatch (params / entity-id pattern ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq pattern (mcp-json-get-string params "pattern"))
  (if (not pattern) (setq pattern "ANSI31"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if ent
    (progn
      (command "_HATCH" "_P" pattern "" "_S" ent "" "")
      (cons T (strcat "{\"entity_type\":\"HATCH\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}")))
    (cons nil "Entity not found for hatching")
  )
)

;; --- Entity query: get ---

(defun mcp-cmd-entity-get (params / entity-id ent ent-data etype handle elayer result)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if (not ent)
    (cons nil (strcat "Entity not found: " entity-id))
    (progn
      (setq ent-data (entget ent))
      (setq etype (cdr (assoc 0 ent-data)))
      (setq handle (cdr (assoc 5 ent-data)))
      (setq elayer (cdr (assoc 8 ent-data)))
      (setq result (strcat "{\"type\":\"" etype "\",\"handle\":\"" handle "\",\"layer\":\"" elayer "\""))
      ;; Add type-specific info
      (cond
        ((= etype "LINE")
         (setq result (strcat result
           ",\"start\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"end\":[" (rtos (car (cdr (assoc 11 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 11 ent-data))) 2 6) "]")))
        ((= etype "CIRCLE")
         (setq result (strcat result
           ",\"center\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"radius\":" (rtos (cdr (assoc 40 ent-data)) 2 6))))
      )
      (setq result (strcat result "}"))
      (cons T result)
    )
  )
)

;; --- Entity modification commands ---

(defun mcp-cmd-entity-copy (params / entity-id dx dy ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq dx (mcp-json-get-number params "dx"))
  (setq dy (mcp-json-get-number params "dy"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.COPY" ent "" '(0 0 0) (list dx dy 0))
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-rotate (params / entity-id cx cy angle ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq angle (mcp-json-get-number params "angle"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn (command "_.ROTATE" ent "" (list cx cy 0) angle) (cons T "\"rotated\""))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-scale (params / entity-id cx cy factor ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq factor (mcp-json-get-number params "factor"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn (command "_.SCALE" ent "" (list cx cy 0) factor) (cons T "\"scaled\""))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-mirror (params / entity-id x1 y1 x2 y2 ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.MIRROR" ent "" (list x1 y1 0) (list x2 y2 0) "_N")
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-offset (params / entity-id distance ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq distance (mcp-json-get-number params "distance"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.OFFSET" distance ent (list 0 0 0) "")
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-array (params / entity-id rows cols row-dist col-dist ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq rows (fix (mcp-json-get-number params "rows")))
  (setq cols (fix (mcp-json-get-number params "cols")))
  (setq row-dist (mcp-json-get-number params "row_dist"))
  (setq col-dist (mcp-json-get-number params "col_dist"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.ARRAY" ent "" "_R" rows cols row-dist col-dist)
      (cons T (strcat "{\"rows\":" (itoa rows) ",\"cols\":" (itoa cols) "}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-fillet (params / id1 id2 radius ent1 ent2)
  (setq id1 (mcp-json-get-string params "id1"))
  (setq id2 (mcp-json-get-string params "id2"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq ent1 (handent id1))
  (setq ent2 (handent id2))
  (if (and ent1 ent2)
    (progn
      (command "_.FILLET" "_R" radius)
      (command "_.FILLET" ent1 ent2)
      (cons T "\"filleted\""))
    (cons nil "One or both entities not found")
  )
)

(defun mcp-cmd-entity-chamfer (params / id1 id2 dist1 dist2 ent1 ent2)
  (setq id1 (mcp-json-get-string params "id1"))
  (setq id2 (mcp-json-get-string params "id2"))
  (setq dist1 (mcp-json-get-number params "dist1"))
  (setq dist2 (mcp-json-get-number params "dist2"))
  (setq ent1 (handent id1))
  (setq ent2 (handent id2))
  (if (and ent1 ent2)
    (progn
      (command "_.CHAMFER" "_D" dist1 dist2)
      (command "_.CHAMFER" ent1 ent2)
      (cons T "\"chamfered\""))
    (cons nil "One or both entities not found")
  )
)

;; --- Layer operations ---

(defun mcp-cmd-layer-set-properties (params / name color linetype lineweight)
  (setq name (mcp-json-get-string params "name"))
  (setq color (mcp-json-get-string params "color"))
  (setq linetype (mcp-json-get-string params "linetype"))
  (setq lineweight (mcp-json-get-string params "lineweight"))
  (if color (command "_.-LAYER" "_COLOR" color name ""))
  (if linetype (command "_.-LAYER" "_LTYPE" linetype name ""))
  (if lineweight (command "_.-LAYER" "_LWEIGHT" lineweight name ""))
  (cons T (strcat "{\"name\":\"" name "\"}"))
)

(defun mcp-cmd-layer-freeze (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_FREEZE" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"frozen\":true}"))
)

(defun mcp-cmd-layer-thaw (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_THAW" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"frozen\":false}"))
)

(defun mcp-cmd-layer-lock (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_LOCK" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"locked\":true}"))
)

(defun mcp-cmd-layer-unlock (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_UNLOCK" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"locked\":false}"))
)

;; --- Block operations (insert-with-attributes, get-attributes, update-attribute) ---

(defun mcp-cmd-block-insert-with-attribs (params / name x y scale rotation attributes ent)
  (setq name (mcp-json-get-string params "name"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if (tblsearch "BLOCK" name)
    (progn
      ;; Insert with ATTREQ=1 to fill attributes
      (command "_.INSERT" name (list x y 0.0) scale scale rotation)
      ;; Note: attribute values are applied separately via update-attribute
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}")))
    (cons nil (strcat "Block '" name "' not found"))
  )
)

(defun mcp-cmd-block-get-attributes (params / entity-id ent sub-ent ent-data attribs)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if (not ent)
    (cons nil "Entity not found")
    (progn
      (setq attribs "" sub-ent (entnext ent))
      (while sub-ent
        (setq ent-data (entget sub-ent))
        (if (= (cdr (assoc 0 ent-data)) "ATTRIB")
          (progn
            (if (> (strlen attribs) 0) (setq attribs (strcat attribs ",")))
            (setq attribs (strcat attribs "\"" (cdr (assoc 2 ent-data)) "\":\"" (mcp-escape-string (cdr (assoc 1 ent-data))) "\""))
          )
        )
        (if (= (cdr (assoc 0 ent-data)) "SEQEND")
          (setq sub-ent nil)
          (setq sub-ent (entnext sub-ent))
        )
      )
      (cons T (strcat "{\"attributes\":{" attribs "}}"))
    )
  )
)

(defun mcp-cmd-block-update-attribute (params / entity-id tag value ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq tag (mcp-json-get-string params "tag"))
  (setq value (mcp-json-get-string params "value"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if (not ent)
    (cons nil "Entity not found")
    (progn
      (if c:update-block-attribute
        (progn (c:update-block-attribute ent tag value)
               (cons T (strcat "{\"tag\":\"" tag "\",\"value\":\"" (mcp-escape-string value) "\"}")))
        ;; Inline fallback if attribute_tools.lsp not loaded
        (progn
          (set_attribute_value ent tag value)
          (cons T (strcat "{\"tag\":\"" tag "\",\"value\":\"" (mcp-escape-string value) "\"}")))
      )
    )
  )
)

;; --- Annotation commands ---

(defun mcp-cmd-create-dimension-linear (params / x1 y1 x2 y2 dim-x dim-y)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq dim-x (mcp-json-get-number params "dim_x"))
  (setq dim-y (mcp-json-get-number params "dim_y"))
  (command "_.DIMLINEAR" (list x1 y1 0) (list x2 y2 0) (list dim-x dim-y 0))
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-dimension-aligned (params / x1 y1 x2 y2 offset)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq offset (mcp-json-get-number params "offset"))
  ;; Place dimension line at offset distance
  (command "_.DIMALIGNED" (list x1 y1 0) (list x2 y2 0)
    (list (+ (/ (+ x1 x2) 2.0) offset) (+ (/ (+ y1 y2) 2.0) offset) 0))
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-dimension-angular (params / cx cy x1 y1 x2 y2)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (command "_.DIMANGULAR" (list cx cy 0) (list x1 y1 0) (list x2 y2 0) "")
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-dimension-radius (params / cx cy radius angle px py)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq angle (mcp-json-get-number params "angle"))
  ;; Need a circle/arc entity first, use entity at center
  (setq px (+ cx (* radius (cos (* angle (/ pi 180.0))))))
  (setq py (+ cy (* radius (sin (* angle (/ pi 180.0))))))
  (command "_.DIMRADIUS" (list px py 0) "")
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-leader (params / text pts-str pairs pt-str)
  (setq text (mcp-json-get-string params "text"))
  (setq pts-str (mcp-json-get-string params "points_str"))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (command "_.LEADER")
      (setq pairs (mcp-split-string pts-str ";"))
      (foreach pt-str pairs
        (command (list (atof (car (mcp-split-string pt-str ",")))
                       (atof (cadr (mcp-split-string pt-str ","))) 0))
      )
      (command "" text "")
      (cons T "{\"entity_type\":\"LEADER\"}")
    )
  )
)

;; --- Drawing management ---

(defun mcp-cmd-drawing-get-variables (params / names-str result var-list var-name var-val first-var)
  (setq names-str (mcp-json-get-string params "names_str"))
  (if (or (not names-str) (= names-str ""))
    ;; Default set when no specific names requested
    (progn
      (setq result "{")
      (setq result (strcat result "\"ACADVER\":\"" (getvar "ACADVER") "\""))
      (setq result (strcat result ",\"DWGNAME\":\"" (mcp-escape-string (getvar "DWGNAME")) "\""))
      (setq result (strcat result ",\"CLAYER\":\"" (getvar "CLAYER") "\""))
      (setq result (strcat result "}"))
      (cons T result)
    )
    ;; Parse semicolon-delimited variable names
    (progn
      (setq var-list (mcp-split-string names-str ";"))
      (setq result "{" first-var T)
      (foreach var-name var-list
        (setq var-val (getvar var-name))
        (if (not first-var) (setq result (strcat result ",")))
        (setq first-var nil)
        (if (not var-val)
          (setq result (strcat result "\"" var-name "\":null"))
          (cond
            ((= (type var-val) 'STR)
             (setq result (strcat result "\"" var-name "\":\"" (mcp-escape-string var-val) "\"")))
            ((= (type var-val) 'INT)
             (setq result (strcat result "\"" var-name "\":" (itoa var-val))))
            ((= (type var-val) 'REAL)
             (setq result (strcat result "\"" var-name "\":" (rtos var-val 2 6))))
            (t
             (setq result (strcat result "\"" var-name "\":\"" (mcp-escape-string (vl-princ-to-string var-val)) "\"")))
          )
        )
      )
      (setq result (strcat result "}"))
      (cons T result)
    )
  )
)

(defun mcp-cmd-drawing-plot-pdf (params / path)
  (setq path (mcp-json-get-string params "path"))
  (if path
    (progn
      (command "_.-PLOT" "_Y" "" "DWG To PDF.pc3"
        "ANSI_A_(8.50_x_11.00_Inches)" "_Inches" "_Landscape"
        "_N" "_Extents" "_Fit" "_Y" "acad.ctb" "_Y" "_N" "_Y" path "_Y")
      (cons T (strcat "{\"path\":\"" (mcp-escape-string path) "\"}")))
    (cons nil "Plot path required")
  )
)

;; --- P&ID list symbols ---

(defun mcp-cmd-pid-list-symbols (params / category dir-path files result)
  (setq category (mcp-json-get-string params "category"))
  (setq dir-path (strcat "C:/PIDv4-CTO/" category "/"))
  (setq files (vl-directory-files dir-path "*.dwg" 1))
  (setq result "")
  (if files
    (foreach f files
      (if (> (strlen result) 0) (setq result (strcat result ",")))
      ;; Remove .dwg extension
      (setq result (strcat result "\"" (substr f 1 (- (strlen f) 4)) "\""))
    )
  )
  (cons T (strcat "{\"category\":\"" category "\",\"symbols\":[" result "],\"count\":" (itoa (length (if files files '()))) "}"))
)

;; --- Block operations ---

(defun mcp-cmd-block-list ( / blk block-list)
  (setq block-list "" blk (tblnext "BLOCK" T))
  (while blk
    (if (not (= (substr (cdr (assoc 2 blk)) 1 1) "*"))
      (progn
        (if (> (strlen block-list) 0)
          (setq block-list (strcat block-list ",\"" (cdr (assoc 2 blk)) "\""))
          (setq block-list (strcat "\"" (cdr (assoc 2 blk)) "\""))
        )
      )
    )
    (setq blk (tblnext "BLOCK"))
  )
  (cons T (strcat "{\"blocks\":[" block-list "]}"))
)

(defun mcp-cmd-library-define (params / name entities-str saved-layer ss count)
  "Render a procedural symbol-library block from a spec mini-language string.

   Parses `entities_str` via `mcp-lib-emit-entities` (defined in
   library_tools.lsp), draws the entities on layer 0, then absorbs them
   into a `_.-BLOCK` named `name`. Returns existed=true and skips emission
   if a block of that name is already in the BLOCK table."
  (setq name (mcp-json-get-string params "name"))
  (setq entities-str (mcp-json-get-string params "entities_str"))
  (cond
    ((or (not name) (= name ""))
     (cons nil "name required"))
    ((tblsearch "BLOCK" name)
     (cons T (strcat "{\"name\":\"" name "\",\"existed\":true,\"entity_count\":0}")))
    ((not *mcp-library-loaded*)
     (cons nil
       (strcat "library_tools.lsp not loaded. Add `lisp-code/` to "
               "AutoCAD's Options -> Files -> Support File Search Path, "
               "or APPLOAD library_tools.lsp for this session.")))
    ((or (not entities-str) (= entities-str ""))
     (cons nil "entities_str required when block doesn't exist"))
    (t
     ;; Emit the primitives on layer "0" so block inserts inherit the
     ;; insert's layer (standard AEC convention; matches COM + ezdxf).
     (setq saved-layer (getvar "CLAYER"))
     (setvar "CLAYER" "0")
     (setq ss (mcp-lib-emit-entities entities-str))
     (setvar "CLAYER" saved-layer)
     (if ss
       (progn
         (setq count (sslength ss))
         ;; `_.-BLOCK` with base point at origin absorbs the selection set
         ;; into a new block definition. The trailing `""` terminates the
         ;; "Select objects" prompt.
         (command "_.-BLOCK" name "0,0" ss "")
         (cons T (strcat "{\"name\":\"" name
                         "\",\"existed\":false,\"entity_count\":"
                         (itoa count) "}")))
       (cons nil "No entities emitted (empty or malformed entities_str)")))))


(defun mcp-cmd-block-insert (params / name x y scale rotation block-id)
  (setq name (mcp-json-get-string params "name"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (setq block-id (mcp-json-get-string params "block_id"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if (tblsearch "BLOCK" name)
    (progn
      (command "_.INSERT" name (list x y 0.0) scale scale rotation)
      (if (and block-id (> (strlen block-id) 0))
        (set_attribute_value (entlast) "ID" block-id)
      )
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
    (cons nil (strcat "Block '" name "' not found"))
  )
)

;; -----------------------------------------------------------------------
;; Main dispatcher — called by "(c:mcp-dispatch)" from Python
;; -----------------------------------------------------------------------

(defun c:mcp-dispatch ( / cmd-files cmd-file json-text request-id cmd-name params-str result result-file)
  "Find pending command file, dispatch, write result."
  ;; Find first pending command file
  (setq cmd-files (vl-directory-files *mcp-ipc-dir* "autocad_mcp_cmd_*.json" 1))
  (if (not cmd-files)
    (progn (princ "\nMCP: No pending commands") (princ))
    (progn
      ;; Process first command
      (setq cmd-file (strcat *mcp-ipc-dir* (car cmd-files)))
      (setq json-text (mcp-read-file-lines cmd-file))

      (if (not json-text)
        (princ "\nMCP: Cannot read command file")
        (progn
          ;; Parse command
          (setq request-id (mcp-json-get-string json-text "request_id"))
          (setq cmd-name (mcp-json-get-string json-text "command"))

          (if (not cmd-name)
            (princ "\nMCP: No command in payload")
            (progn
              (princ (strcat "\nMCP: Dispatching " cmd-name " [" request-id "]"))

              ;; Execute via whitelist dispatcher
              (setq result
                (vl-catch-all-apply
                  'mcp-dispatch-command
                  (list cmd-name json-text)
                )
              )

              ;; Handle error from vl-catch-all-apply
              (if (vl-catch-all-error-p result)
                (setq result (cons nil (vl-catch-all-error-message result)))
              )

              ;; Write result
              (setq result-file (strcat *mcp-ipc-dir* "autocad_mcp_result_" request-id ".json"))
              (if (car result)
                (mcp-write-result result-file request-id T (cdr result) nil)
                (mcp-write-result result-file request-id nil nil (cdr result))
              )

              (princ (strcat "\nMCP: Done " cmd-name))
            )
          )

          ;; Clean up command file
          (vl-file-delete cmd-file)
        )
      )
    )
  )
  (princ)
)

;; -----------------------------------------------------------------------
;; Utility helpers (defined if not already loaded from external files)
;; -----------------------------------------------------------------------

(if (not ensure_layer_exists)
  (defun ensure_layer_exists (name color linetype)
    "Create layer if it doesn't exist."
    (if (not (tblsearch "LAYER" name))
      (command "_.-LAYER" "_NEW" name "_COLOR" color name "_LTYPE" linetype name "")
    )
  )
)

(if (not set_current_layer)
  (defun set_current_layer (name)
    "Set a layer as current."
    (setvar "CLAYER" name)
  )
)

(if (not set_attribute_value)
  (defun set_attribute_value (ent tag value / sub-ent ent-data)
    "Set an attribute value on a block insert by tag name."
    (setq sub-ent (entnext ent))
    (while sub-ent
      (setq ent-data (entget sub-ent))
      (if (and (= (cdr (assoc 0 ent-data)) "ATTRIB")
               (= (strcase (cdr (assoc 2 ent-data))) (strcase tag)))
        (progn
          (entmod (subst (cons 1 value) (assoc 1 ent-data) ent-data))
          (entupd sub-ent)
          (setq sub-ent nil)  ; stop
        )
        (if (= (cdr (assoc 0 ent-data)) "SEQEND")
          (setq sub-ent nil)
          (setq sub-ent (entnext sub-ent))
        )
      )
    )
  )
)

;; -----------------------------------------------------------------------
;; Architectural primitives (AIA/NCS) — wire-compatible with the COM + ezdxf
;; backends. Geometry math mirrors `src/autocad_mcp/arch/geometry.py`.
;; -----------------------------------------------------------------------

;; AIA layer table as a flat list of triples: (name color linetype) ...
(setq *mcp-aia-layers* (list
  "A-WALL" 4 "CONTINUOUS"
  "A-WALL-PATT" 251 "CONTINUOUS"
  "A-WALL-HEAD" 4 "DASHED"          ; HIDDEN aliased to DASHED, matches ezdxf path
  "A-DOOR" 2 "CONTINUOUS"
  "A-GLAZ" 5 "CONTINUOUS"
  "A-FLOR" 3 "CONTINUOUS"
  "A-FLOR-FIXT" 3 "CONTINUOUS"
  "A-FLOR-CASE" 30 "CONTINUOUS"
  "A-FLOR-STRS" 6 "CONTINUOUS"
  "A-AREA" 1 "CONTINUOUS"
  "A-AREA-IDEN" 1 "CONTINUOUS"
  "A-ANNO-DIMS" 2 "CONTINUOUS"
  "A-ANNO-TEXT" 7 "CONTINUOUS"
  "A-ANNO-NOTE" 7 "CONTINUOUS"
  "A-ANNO-SYMB" 4 "CONTINUOUS"
  "A-ANNO-TTLB" 7 "CONTINUOUS"
  "A-GRID" 8 "CENTER"
  "A-CLNG" 6 "CONTINUOUS"
  "A-ROOF" 6 "CONTINUOUS"
))

(defun mcp-cmd-arch-setup-layers ( / specs name color linetype created count)
  "Create the AIA standard layer set. Idempotent.
   Returns a JSON payload with `count` and the list of created names."
  (setq specs *mcp-aia-layers*)
  (setq created "" count 0)
  (while specs
    (setq name (car specs)
          color (cadr specs)
          linetype (caddr specs)
          specs (cdddr specs))
    (if (not (tblsearch "LAYER" name))
      (progn
        ;; Load the linetype from acad.lin if needed; fallback to CONTINUOUS.
        (if (and linetype (/= linetype "CONTINUOUS") (not (tblsearch "LTYPE" linetype)))
          (command "_.-LINETYPE" "_L" linetype "acad.lin" "")
        )
        (ensure_layer_exists name color
          (if (tblsearch "LTYPE" linetype) linetype "CONTINUOUS"))
        (setq count (1+ count))
        (setq created
          (if (= (strlen created) 0)
            (strcat "\"" name "\"")
            (strcat created ",\"" name "\"")))
      )
    )
  )
  (cons T (strcat "{\"created\":[" created "],\"count\":" (itoa count) "}"))
)

;; Helper: compute a unit perpendicular (CCW from direction) of a 2D vector.
(defun mcp-perp-unit (dx dy / len)
  "Return (-dy/len . dx/len) for a vector (dx, dy). Caller must ensure non-zero length."
  (setq len (sqrt (+ (* dx dx) (* dy dy))))
  (cons (/ (- 0 dy) len) (/ dx len))
)

(defun mcp-cmd-arch-wall (params / x1 y1 x2 y2 thickness layer justify
                                   dx dy len perp nx ny off-l off-r
                                   p1 p2 p3 p4)
  "Draw a wall as a closed rectangle along (x1,y1)→(x2,y2)."
  (setq x1 (mcp-json-get-number params "x1")
        y1 (mcp-json-get-number params "y1")
        x2 (mcp-json-get-number params "x2")
        y2 (mcp-json-get-number params "y2")
        thickness (mcp-json-get-number params "thickness")
        layer (mcp-json-get-string params "layer")
        justify (mcp-json-get-string params "justify"))
  (if (not thickness) (setq thickness 6.0))
  (if (not layer) (setq layer "A-WALL"))
  (if (not justify) (setq justify "center"))
  (setq dx (- x2 x1) dy (- y2 y1))
  (setq len (sqrt (+ (* dx dx) (* dy dy))))
  (if (= len 0.0)
    (cons nil "Wall has zero length")
    (progn
      (setq perp (mcp-perp-unit dx dy))
      (setq nx (car perp) ny (cdr perp))
      (cond
        ((= justify "center") (setq off-l (/ thickness 2.0) off-r (/ thickness 2.0)))
        ((= justify "left")   (setq off-l thickness off-r 0.0))
        ((= justify "right")  (setq off-l 0.0 off-r thickness))
        (t (setq off-l (/ thickness 2.0) off-r (/ thickness 2.0))))
      (setq p1 (list (+ x1 (* nx off-l)) (+ y1 (* ny off-l)) 0.0)
            p2 (list (+ x2 (* nx off-l)) (+ y2 (* ny off-l)) 0.0)
            p3 (list (- x2 (* nx off-r)) (- y2 (* ny off-r)) 0.0)
            p4 (list (- x1 (* nx off-r)) (- y1 (* ny off-r)) 0.0))
      (ensure_layer_exists layer 4 "CONTINUOUS")
      (set_current_layer layer)
      (command "_.PLINE" p1 p2 p3 p4 "_C")
      (cons T (strcat "{\"entity_type\":\"LWPOLYLINE\",\"handle\":\""
                      (cdr (assoc 5 (entget (entlast))))
                      "\",\"layer\":\"" layer
                      "\",\"length\":" (rtos len 2 6) "}"))
    )
  )
)

;; Draw one wall segment between centerline positions `seg-start` and `seg-end`.
;; Uses the precomputed unit normal (nx,ny) and offsets off-l/off-r from the
;; caller. Returns the entity handle of the resulting closed polyline.
(defun mcp-wall-segment-draw (x1 y1 ux uy nx ny seg-start seg-end off-l off-r
                              layer / sx sy ex ey p1 p2 p3 p4)
  (setq sx (+ x1 (* ux seg-start)) sy (+ y1 (* uy seg-start)))
  (setq ex (+ x1 (* ux seg-end))   ey (+ y1 (* uy seg-end)))
  (setq p1 (list (+ sx (* nx off-l)) (+ sy (* ny off-l)) 0.0)
        p2 (list (+ ex (* nx off-l)) (+ ey (* ny off-l)) 0.0)
        p3 (list (- ex (* nx off-r)) (- ey (* ny off-r)) 0.0)
        p4 (list (- sx (* nx off-r)) (- sy (* ny off-r)) 0.0))
  (set_current_layer layer)
  (command "_.PLINE" p1 p2 p3 p4 "_C")
  (cdr (assoc 5 (entget (entlast))))
)

;; Insertion-sort a list of opening records (each a list (pos width type
;; swing open-angle sill-thickness)) by position. Stable enough for typical
;; opening counts (1-10 per wall).
(defun mcp-sort-openings (openings / sorted o inserted scan acc)
  (setq sorted nil)
  (foreach o openings
    (setq inserted nil acc nil scan sorted)
    (while scan
      (if (and (not inserted) (< (car o) (car (car scan))))
        (progn (setq acc (append acc (list o))) (setq inserted T))
      )
      (setq acc (append acc (list (car scan))))
      (setq scan (cdr scan))
    )
    (if (not inserted) (setq acc (append acc (list o))))
    (setq sorted acc)
  )
  sorted
)

;; Parse the openings_str payload into a sorted list of canonical records.
;; Format: "pos|width|type|swing|open_angle|sill_thickness;..." — empty
;; fields take per-type defaults (door: swing=left, open_angle=90; window:
;; sill_thickness=<wall thickness>).
(defun mcp-parse-openings (openings-str wall-thickness / records entries fields
                                          pos width kind swing op-ang sill)
  (setq records nil)
  (if (and openings-str (> (strlen openings-str) 0))
    (progn
      (setq entries (mcp-split-string openings-str ";"))
      (foreach entry entries
        (if (> (strlen entry) 0)
          (progn
            (setq fields (mcp-split-string entry "|"))
            (setq pos    (atof (nth 0 fields)))
            (setq width  (atof (nth 1 fields)))
            (setq kind   (if (>= (length fields) 3) (nth 2 fields) "door"))
            (if (or (null kind) (= kind "")) (setq kind "door"))
            (setq swing  (if (>= (length fields) 4) (nth 3 fields) "left"))
            (if (or (null swing) (= swing "")) (setq swing "left"))
            (setq op-ang (if (and (>= (length fields) 5)
                                  (> (strlen (nth 4 fields)) 0))
                            (atof (nth 4 fields))
                            90.0))
            (setq sill   (if (and (>= (length fields) 6)
                                  (> (strlen (nth 5 fields)) 0))
                            (atof (nth 5 fields))
                            wall-thickness))
            (setq records (append records
                            (list (list pos width kind swing op-ang sill))))
          )
        )
      )
    )
  )
  (mcp-sort-openings records)
)

(defun mcp-cmd-arch-wall-with-openings (params / x1 y1 x2 y2 thickness layer
                                          door-layer window-layer justify
                                          openings-str openings dx dy total-len
                                          ux uy perp nx ny off-l off-r
                                          angle-deg cursor seg-handles
                                          door-records window-records
                                          opening-rec pos width kind swing
                                          op-ang sill start-pos end-pos
                                          cs-x cs-y ce-x ce-y handle
                                          leaf-dir leaf-rad lx ly arc-start
                                          arc-end leaf-h arc-h half
                                          ws-p1 ws-p2 ws-p3 ws-p4 sill-h glass-h
                                          json-segs json-doors json-windows)
  "Wall split into solid segments around door/window openings."
  (setq x1 (mcp-json-get-number params "x1")
        y1 (mcp-json-get-number params "y1")
        x2 (mcp-json-get-number params "x2")
        y2 (mcp-json-get-number params "y2")
        thickness (mcp-json-get-number params "thickness")
        layer (mcp-json-get-string params "layer")
        door-layer (mcp-json-get-string params "door_layer")
        window-layer (mcp-json-get-string params "window_layer")
        justify (mcp-json-get-string params "justify")
        openings-str (mcp-json-get-string params "openings_str"))
  (if (not thickness) (setq thickness 6.0))
  (if (not layer) (setq layer "A-WALL"))
  (if (not door-layer) (setq door-layer "A-DOOR"))
  (if (not window-layer) (setq window-layer "A-GLAZ"))
  (if (not justify) (setq justify "center"))

  (setq dx (- x2 x1) dy (- y2 y1))
  (setq total-len (sqrt (+ (* dx dx) (* dy dy))))
  (if (= total-len 0.0)
    (cons nil "Wall has zero length")
    (progn
      (setq ux (/ dx total-len) uy (/ dy total-len))
      (setq perp (mcp-perp-unit dx dy))
      (setq nx (car perp) ny (cdr perp))
      (setq angle-deg (* (atan dy dx) (/ 180.0 pi)))
      (cond
        ((= justify "center") (setq off-l (/ thickness 2.0) off-r (/ thickness 2.0)))
        ((= justify "left")   (setq off-l thickness off-r 0.0))
        ((= justify "right")  (setq off-l 0.0 off-r thickness))
        (t (setq off-l (/ thickness 2.0) off-r (/ thickness 2.0))))

      (ensure_layer_exists layer 4 "CONTINUOUS")
      (ensure_layer_exists door-layer 2 "CONTINUOUS")
      (ensure_layer_exists window-layer 5 "CONTINUOUS")

      (setq openings (mcp-parse-openings openings-str thickness))
      ;; Validate openings stay inside the wall and don't overlap. Bail
      ;; before drawing anything to match the Python behaviour.
      (setq cursor 0.0)
      (foreach opening-rec openings
        (setq pos (nth 0 opening-rec) width (nth 1 opening-rec))
        (setq start-pos (- pos (/ width 2.0)))
        (setq end-pos   (+ pos (/ width 2.0)))
        (cond
          ((<= width 0.0)
           (setq openings nil cursor -1.0))
          ((or (< start-pos 0.0) (> end-pos total-len))
           (setq openings nil cursor -1.0))
          ((< start-pos cursor)
           (setq openings nil cursor -1.0))
          (t (setq cursor end-pos))
        )
      )
      (if (< cursor 0.0)
        (cons nil "Opening invalid (overlapping, out of range, or zero width)")
        (progn
          ;; Draw solid segments between openings.
          (setq seg-handles nil cursor 0.0)
          (foreach opening-rec openings
            (setq pos (nth 0 opening-rec) width (nth 1 opening-rec))
            (setq start-pos (- pos (/ width 2.0))
                  end-pos   (+ pos (/ width 2.0)))
            (if (> start-pos cursor)
              (setq seg-handles
                (append seg-handles
                  (list (mcp-wall-segment-draw x1 y1 ux uy nx ny
                          cursor start-pos off-l off-r layer)))))
            (setq cursor end-pos)
          )
          (if (< cursor total-len)
            (setq seg-handles
              (append seg-handles
                (list (mcp-wall-segment-draw x1 y1 ux uy nx ny
                        cursor total-len off-l off-r layer)))))
          (if (null openings)
            ;; No openings — keep the original single-rectangle behaviour.
            (setq seg-handles
              (list (mcp-wall-segment-draw x1 y1 ux uy nx ny
                      0.0 total-len off-l off-r layer))))

          ;; Insert door / window symbols at each opening.
          (setq door-records nil window-records nil)
          (foreach opening-rec openings
            (setq pos    (nth 0 opening-rec)
                  width  (nth 1 opening-rec)
                  kind   (nth 2 opening-rec)
                  swing  (nth 3 opening-rec)
                  op-ang (nth 4 opening-rec)
                  sill   (nth 5 opening-rec))
            (setq start-pos (- pos (/ width 2.0))
                  end-pos   (+ pos (/ width 2.0)))
            (setq cs-x (+ x1 (* ux start-pos))
                  cs-y (+ y1 (* uy start-pos))
                  ce-x (+ x1 (* ux end-pos))
                  ce-y (+ y1 (* uy end-pos)))
            (cond
              ((= kind "door")
               (set_current_layer door-layer)
               (setq leaf-dir (+ angle-deg
                                 (if (= swing "left") op-ang (- op-ang))))
               (setq leaf-rad (* leaf-dir (/ pi 180.0)))
               (setq lx (+ cs-x (* width (cos leaf-rad)))
                     ly (+ cs-y (* width (sin leaf-rad))))
               (if (= swing "left")
                 (setq arc-start angle-deg arc-end (+ angle-deg op-ang))
                 (setq arc-start (- angle-deg op-ang) arc-end angle-deg))
               (command "_.LINE" (list cs-x cs-y 0.0) (list lx ly 0.0) "")
               (setq leaf-h (cdr (assoc 5 (entget (entlast)))))
               (command "_.ARC" "_C" (list cs-x cs-y 0.0)
                        (list (+ cs-x (* width (cos (* arc-start (/ pi 180.0)))))
                              (+ cs-y (* width (sin (* arc-start (/ pi 180.0))))) 0.0)
                        "_A" (- arc-end arc-start))
               (setq arc-h (cdr (assoc 5 (entget (entlast)))))
               (setq door-records
                 (append door-records
                   (list (strcat "{\"leaf\":\"" leaf-h
                                 "\",\"arc\":\"" arc-h "\"}"))))
              )
              ((= kind "window")
               (set_current_layer window-layer)
               (setq half (/ sill 2.0))
               (setq ws-p1 (list (+ cs-x (* nx half)) (+ cs-y (* ny half)) 0.0)
                     ws-p2 (list (+ ce-x (* nx half)) (+ ce-y (* ny half)) 0.0)
                     ws-p3 (list (- ce-x (* nx half)) (- ce-y (* ny half)) 0.0)
                     ws-p4 (list (- cs-x (* nx half)) (- cs-y (* ny half)) 0.0))
               (command "_.PLINE" ws-p1 ws-p2 ws-p3 ws-p4 "_C")
               (setq sill-h (cdr (assoc 5 (entget (entlast)))))
               (command "_.LINE" (list cs-x cs-y 0.0) (list ce-x ce-y 0.0) "")
               (setq glass-h (cdr (assoc 5 (entget (entlast)))))
               (setq window-records
                 (append window-records
                   (list (strcat "{\"sill\":\"" sill-h
                                 "\",\"glass\":\"" glass-h "\"}"))))
              )
            )
          )

          ;; Build the response payload — arrays of segment handles, door
          ;; symbol handle objects, and window symbol handle objects.
          (setq json-segs "")
          (foreach handle seg-handles
            (if (> (strlen json-segs) 0)
              (setq json-segs (strcat json-segs ",\"" handle "\""))
              (setq json-segs (strcat "\"" handle "\""))))
          (setq json-doors "")
          (foreach handle door-records
            (if (> (strlen json-doors) 0)
              (setq json-doors (strcat json-doors "," handle))
              (setq json-doors handle)))
          (setq json-windows "")
          (foreach handle window-records
            (if (> (strlen json-windows) 0)
              (setq json-windows (strcat json-windows "," handle))
              (setq json-windows handle)))

          (cons T (strcat "{\"handles\":{\"segments\":[" json-segs
                          "],\"doors\":[" json-doors
                          "],\"windows\":[" json-windows
                          "]},\"layer\":\"" layer
                          "\",\"length\":" (rtos total-len 2 6)
                          ",\"thickness\":" (rtos thickness 2 6)
                          ",\"segment_count\":" (itoa (length seg-handles))
                          ",\"opening_count\":" (itoa (length openings)) "}"))
        )
      )
    )
  )
)

(defun mcp-cmd-arch-door (params / x y width angle swing open-angle layer
                                   rs-x rs-y dir-x dir-y rsx-rel rsy-rel cross
                                   leaf-dir leaf-rad lx ly arc-start arc-end
                                   leaf-handle arc-handle)
  "Door = leaf line from hinge to its open endpoint + swing arc.
   If room_side_x AND room_side_y are provided, override `swing` so the
   door arc sweeps toward that point — matches the Python door_geometry."
  (setq x (mcp-json-get-number params "x")
        y (mcp-json-get-number params "y")
        width (mcp-json-get-number params "width")
        angle (mcp-json-get-number params "angle")
        swing (mcp-json-get-string params "swing")
        open-angle (mcp-json-get-number params "open_angle")
        layer (mcp-json-get-string params "layer")
        rs-x (mcp-json-get-number params "room_side_x")
        rs-y (mcp-json-get-number params "room_side_y"))
  (if (not width) (setq width 36.0))
  (if (not angle) (setq angle 0.0))
  (if (not swing) (setq swing "left"))
  (if (not open-angle) (setq open-angle 90.0))
  (if (not layer) (setq layer "A-DOOR"))

  ;; If both room_side coords were sent, override `swing` via cross product.
  (if (and rs-x rs-y)
    (progn
      (setq dir-x (cos (* angle (/ pi 180.0)))
            dir-y (sin (* angle (/ pi 180.0))))
      (setq rsx-rel (- rs-x x)
            rsy-rel (- rs-y y))
      (setq cross (- (* dir-x rsy-rel) (* dir-y rsx-rel)))
      (cond
        ((> cross 0) (setq swing "left"))
        ((< cross 0) (setq swing "right")))
    )
  )

  (ensure_layer_exists layer 2 "CONTINUOUS")
  (set_current_layer layer)

  ;; Leaf endpoint at full open
  (setq leaf-dir (+ angle (if (= swing "left") open-angle (- open-angle))))
  (setq leaf-rad (* leaf-dir (/ pi 180.0)))
  (setq lx (+ x (* width (cos leaf-rad)))
        ly (+ y (* width (sin leaf-rad))))

  ;; Arc: AutoCAD arcs are CCW. For "right" swing we still need CCW so flip
  ;; the start/end pair.
  (if (= swing "left")
    (setq arc-start angle arc-end (+ angle open-angle))
    (setq arc-start (- angle open-angle) arc-end angle))

  (command "_.LINE" (list x y 0.0) (list lx ly 0.0) "")
  (setq leaf-handle (cdr (assoc 5 (entget (entlast)))))
  ;; ARC center, start angle, end angle (degrees in the LISP command line)
  (command "_.ARC" "_C" (list x y 0.0)
           (list (+ x (* width (cos (* arc-start (/ pi 180.0)))))
                 (+ y (* width (sin (* arc-start (/ pi 180.0))))) 0.0)
           "_A" (- arc-end arc-start))
  (setq arc-handle (cdr (assoc 5 (entget (entlast)))))
  (cons T (strcat "{\"handles\":{\"leaf\":\"" leaf-handle
                  "\",\"arc\":\"" arc-handle
                  "\"},\"layer\":\"" layer
                  "\",\"width\":" (rtos width 2 6) "}"))
)

(defun mcp-cmd-arch-window (params / x1 y1 x2 y2 sill-thickness layer
                                     dx dy len perp nx ny half
                                     p1 p2 p3 p4 sill-handle glass-handle)
  "Window = sill rectangle + center glass line."
  (setq x1 (mcp-json-get-number params "x1")
        y1 (mcp-json-get-number params "y1")
        x2 (mcp-json-get-number params "x2")
        y2 (mcp-json-get-number params "y2")
        sill-thickness (mcp-json-get-number params "sill_thickness")
        layer (mcp-json-get-string params "layer"))
  (if (not sill-thickness) (setq sill-thickness 6.0))
  (if (not layer) (setq layer "A-GLAZ"))
  (setq dx (- x2 x1) dy (- y2 y1))
  (setq len (sqrt (+ (* dx dx) (* dy dy))))
  (if (= len 0.0)
    (cons nil "Window has zero length")
    (progn
      (setq perp (mcp-perp-unit dx dy))
      (setq nx (car perp) ny (cdr perp))
      (setq half (/ sill-thickness 2.0))
      (setq p1 (list (+ x1 (* nx half)) (+ y1 (* ny half)) 0.0)
            p2 (list (+ x2 (* nx half)) (+ y2 (* ny half)) 0.0)
            p3 (list (- x2 (* nx half)) (- y2 (* ny half)) 0.0)
            p4 (list (- x1 (* nx half)) (- y1 (* ny half)) 0.0))
      (ensure_layer_exists layer 5 "CONTINUOUS")
      (set_current_layer layer)
      (command "_.PLINE" p1 p2 p3 p4 "_C")
      (setq sill-handle (cdr (assoc 5 (entget (entlast)))))
      (command "_.LINE" (list x1 y1 0.0) (list x2 y2 0.0) "")
      (setq glass-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handles\":{\"sill\":\"" sill-handle
                      "\",\"glass\":\"" glass-handle
                      "\"},\"layer\":\"" layer
                      "\",\"width\":" (rtos len 2 6) "}"))
    )
  )
)

(defun mcp-cmd-arch-room (params / pts-str name area-layer text-layer text-height
                                   pairs pt-str cx cy points raw-area area
                                   centroid-x centroid-y cross
                                   prev-x prev-y label-text bnd-handle lbl-handle
                                   insunits display-area suffix area-str)
  "Room = closed boundary polyline on area-layer + name/area label at centroid."
  (setq pts-str (mcp-json-get-string params "points_str")
        name (mcp-json-get-string params "name")
        area-layer (mcp-json-get-string params "area_layer")
        text-layer (mcp-json-get-string params "text_layer")
        text-height (mcp-json-get-number params "text_height"))
  (if (not area-layer) (setq area-layer "A-AREA"))
  (if (not text-layer) (setq text-layer "A-AREA-IDEN"))
  (if (not text-height) (setq text-height 6.0))
  (if (not name) (setq name ""))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (ensure_layer_exists area-layer 1 "CONTINUOUS")
      (ensure_layer_exists text-layer 1 "CONTINUOUS")
      (set_current_layer area-layer)

      ;; Parse points into a list
      (setq pairs (mcp-split-string pts-str ";"))
      (setq points nil)
      (foreach pt-str pairs
        (setq cx (atof (car (mcp-split-string pt-str ","))))
        (setq cy (atof (cadr (mcp-split-string pt-str ","))))
        (setq points (append points (list (list cx cy))))
      )

      ;; Draw the closed polyline by replaying the points to PLINE.
      (command "_.PLINE")
      (foreach pt points
        (command (list (car pt) (cadr pt) 0.0)))
      (command "_C")
      (setq bnd-handle (cdr (assoc 5 (entget (entlast)))))

      ;; Shoelace signed sum + centroid weights.
      ;; raw-area = sum of cross products = 2*signed_area.
      ;; centroid_x = sum_x_terms / (3*raw-area), same for y.
      (setq raw-area 0.0 centroid-x 0.0 centroid-y 0.0)
      (setq prev-x (car (last points)) prev-y (cadr (last points)))
      (foreach pt points
        (setq cross (- (* prev-x (cadr pt)) (* (car pt) prev-y)))
        (setq raw-area (+ raw-area cross))
        (setq centroid-x (+ centroid-x (* (+ prev-x (car pt)) cross)))
        (setq centroid-y (+ centroid-y (* (+ prev-y (cadr pt)) cross)))
        (setq prev-x (car pt) prev-y (cadr pt))
      )
      (setq area (/ (abs raw-area) 2.0))
      (if (= raw-area 0.0)
        ;; Degenerate (collinear) polygon → arithmetic mean fallback.
        (progn
          (setq centroid-x 0.0 centroid-y 0.0)
          (foreach pt points
            (setq centroid-x (+ centroid-x (car pt))
                  centroid-y (+ centroid-y (cadr pt))))
          (setq centroid-x (/ centroid-x (length points))
                centroid-y (/ centroid-y (length points))))
        (progn
          (setq centroid-x (/ centroid-x (* 3.0 raw-area))
                centroid-y (/ centroid-y (* 3.0 raw-area)))))

      (set_current_layer text-layer)
      ;; Choose area suffix from $INSUNITS so metric drawings get "m²".
      ;; Imperial inches (1): area is in², display as ft² → divide by 144.
      ;; Imperial feet (2):   already ft², display as SF.
      ;; Millimeters (4):     mm², display as m² → divide by 1,000,000.
      ;; Centimeters (5):     cm², display as m² → divide by 10,000.
      ;; Meters (6):          already m².
      ;; Anything else: drop the suffix, raw number.
      (setq insunits (getvar "INSUNITS"))
      (cond
        ((= insunits 1) (setq display-area (/ area 144.0) suffix "SF"))
        ((= insunits 2) (setq display-area area suffix "SF"))
        ((= insunits 4) (setq display-area (/ area 1000000.0) suffix "m²"))
        ((= insunits 5) (setq display-area (/ area 10000.0) suffix "m²"))
        ((= insunits 6) (setq display-area area suffix "m²"))
        (t              (setq display-area area suffix "")))
      (setq area-str
        (if (>= (abs display-area) 100.0)
          (rtos display-area 2 0)
          (rtos display-area 2 1)))
      (if (> (strlen suffix) 0)
        (setq area-str (strcat area-str " " suffix)))
      (setq label-text
        (if (> (strlen name) 0)
          (strcat name "\\P" area-str)
          area-str))
      (command "_.MTEXT" (list centroid-x centroid-y 0.0)
               "_H" text-height "_J" "MC" "_W" 0 label-text "")
      (setq lbl-handle (cdr (assoc 5 (entget (entlast)))))

      (cons T (strcat "{\"handles\":{\"boundary\":\"" bnd-handle
                      "\",\"label\":\"" lbl-handle
                      "\"},\"layer\":\"" area-layer
                      "\",\"area\":" (rtos area 2 4)
                      ",\"name\":\"" (mcp-escape-string name) "\"}"))
    )
  )
)

(defun mcp-cmd-arch-dimension-chain (params / pts-str offset direction layer
                                              pairs pt-str cx cy points
                                              n i p1 p2 base-x base-y count)
  "Chain of linear/aligned dimensions across consecutive point pairs."
  (setq pts-str (mcp-json-get-string params "points_str")
        offset (mcp-json-get-number params "offset")
        direction (mcp-json-get-string params "direction")
        layer (mcp-json-get-string params "layer"))
  (if (not offset) (setq offset 24.0))
  (if (not direction) (setq direction "horizontal"))
  (if (not layer) (setq layer "A-ANNO-DIMS"))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (ensure_layer_exists layer 2 "CONTINUOUS")
      (set_current_layer layer)
      (setq pairs (mcp-split-string pts-str ";"))
      (setq points nil)
      (foreach pt-str pairs
        (setq cx (atof (car (mcp-split-string pt-str ","))))
        (setq cy (atof (cadr (mcp-split-string pt-str ","))))
        (setq points (append points (list (list cx cy)))))
      (setq n (length points))
      (if (< n 2)
        (cons nil "Need >=2 points for a dimension chain")
        (progn
          (setq i 0 count 0)
          (while (< i (- n 1))
            (setq p1 (nth i points) p2 (nth (+ i 1) points))
            (cond
              ((= direction "horizontal")
               (setq base-x (/ (+ (car p1) (car p2)) 2.0))
               (setq base-y (+ (max (cadr p1) (cadr p2)) offset))
               (command "_.DIMLINEAR"
                        (list (car p1) (cadr p1) 0)
                        (list (car p2) (cadr p2) 0)
                        (list base-x base-y 0)))
              ((= direction "vertical")
               (setq base-x (+ (max (car p1) (car p2)) offset))
               (setq base-y (/ (+ (cadr p1) (cadr p2)) 2.0))
               (command "_.DIMLINEAR"
                        (list (car p1) (cadr p1) 0)
                        (list (car p2) (cadr p2) 0)
                        (list base-x base-y 0)))
              (t  ; "aligned"
               (command "_.DIMALIGNED"
                        (list (car p1) (cadr p1) 0)
                        (list (car p2) (cadr p2) 0)
                        (list (/ (+ (car p1) (car p2)) 2.0)
                              (+ (/ (+ (cadr p1) (cadr p2)) 2.0) offset) 0))))
            (setq count (1+ count))
            (setq i (1+ i)))
          (cons T (strcat "{\"count\":" (itoa count)
                          ",\"layer\":\"" layer "\"}"))
        )
      )
    )
  )
)

;; -----------------------------------------------------------------------
;; Auto-load companion files
;; -----------------------------------------------------------------------

;; Try to load pid_tools.lsp via the AutoCAD support file search path
;; (Options → Files → Support File Search Path). Without it every `pid_*`
;; operation returns "pid_tools.lsp not loaded" — historically the file
;; was referenced but never shipped at all.
;;
;; Auto-load works when the lisp-code/ directory is on the support path.
;; If only TRUSTEDPATHS is set, `findfile` won't see the file — APPLOAD
;; pid_tools.lsp once and AutoCAD remembers it for the session.
(setq *mcp-pid-tools-path* (findfile "pid_tools.lsp"))
(if *mcp-pid-tools-path*
  (load *mcp-pid-tools-path*)
  (princ (strcat
    "\nMCP: pid_tools.lsp not found on AutoCAD's support path. "
    "`pid` operations will return 'not loaded' until you either "
    "APPLOAD pid_tools.lsp manually or add the lisp-code/ directory "
    "to Options → Files → Support File Search Path.")))

;; Same pattern for library_tools.lsp — when present it defines
;; `mcp-lib-emit-entities` (the renderer for serialized block specs sent
;; from Python's file_ipc backend) and sets `*mcp-library-loaded*`. Without
;; it, `library-define` returns an actionable error pointing at the
;; support path setting.
(setq *mcp-library-tools-path* (findfile "library_tools.lsp"))
(if *mcp-library-tools-path*
  (load *mcp-library-tools-path*)
  (princ (strcat
    "\nMCP: library_tools.lsp not found on AutoCAD's support path. "
    "`library` operations will return 'not loaded' until you either "
    "APPLOAD library_tools.lsp manually or add the lisp-code/ directory "
    "to Options → Files → Support File Search Path.")))

;; Same pattern for arch_tools.lsp — defines `mcp-arch-emit-entities` (the
;; renderer for serialized stair/column/slab/beam/roof/elevation/section/
;; template specs sent from Python's file_ipc backend) and the dispatch
;; entry `mcp-cmd-arch-emit`. Without it, the new arch primitives fall back
;; to "not loaded" with an actionable error.
(setq *mcp-arch-tools-path* (findfile "arch_tools.lsp"))
(if *mcp-arch-tools-path*
  (load *mcp-arch-tools-path*)
  (princ (strcat
    "\nMCP: arch_tools.lsp not found on AutoCAD's support path. "
    "`arch.stair/column/slab/beam/roof/elevation/section/template` "
    "operations will return 'not loaded' until you either APPLOAD "
    "arch_tools.lsp manually or add the lisp-code/ directory to "
    "Options → Files → Support File Search Path.")))

;; -----------------------------------------------------------------------
;; Startup message
;; -----------------------------------------------------------------------

(princ "\n=== MCP Dispatch v3.2 loaded ===")
(princ "\nIPC directory: ")
(princ *mcp-ipc-dir*)
(princ "\nReady for commands via (c:mcp-dispatch)")
(princ)
