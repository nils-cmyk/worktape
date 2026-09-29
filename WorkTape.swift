// WorkTape — records the screen only while work apps are in front.
// Frames every N seconds → hourly HEVC video + a TSV log of app/window titles.

import Cocoa
import ScreenCaptureKit
import WebKit

// ~/WorkTape/config.json. Any key left out keeps its default, so older files keep working after an update.
struct Config: Codable {
    var apps: [String] = ["com.anthropic.claudefordesktop"]          // always recorded while in front
    // Browsers to record, by bundle id -> profile names. ["*"] = every window. Named profiles (Chromium
    // browsers only) are matched against the profile button in the toolbar and need Accessibility permission.
    var browsers: [String: [String]] = [
        "com.google.Chrome": ["*"], "com.brave.Browser": ["*"], "company.thebrowser.Browser": ["*"],
        "com.microsoft.edgemac": ["*"], "com.apple.Safari": ["*"],
    ]
    var terminalApps: [String] = ["com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty",
                                  "dev.warp.Warp-Stable"]
    var terminalTitleContains: [String] = ["claude"]    // record terminals only when running Claude Code
    var intervalSeconds: Double = 2
    var idleSeconds: Double = 300
    var retentionDays: Int = 30
    var maxWidth: Int = 1920
    var ffmpeg: String = ""                              // empty = find it (Homebrew on Apple Silicon or Intel)

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = Config()
        apps = try c.decodeIfPresent([String].self, forKey: .apps) ?? d.apps
        browsers = try c.decodeIfPresent([String: [String]].self, forKey: .browsers) ?? d.browsers
        // older configs: "braveProfiles": ["Work"] meant "only Brave, only that profile"
        if !c.contains(.browsers), let brave = try c.decodeIfPresent([String].self, forKey: .braveProfiles) {
            browsers = ["com.brave.Browser": brave]
        }
        terminalApps = try c.decodeIfPresent([String].self, forKey: .terminalApps) ?? d.terminalApps
        terminalTitleContains = try c.decodeIfPresent([String].self, forKey: .terminalTitleContains) ?? d.terminalTitleContains
        intervalSeconds = try c.decodeIfPresent(Double.self, forKey: .intervalSeconds) ?? d.intervalSeconds
        idleSeconds = try c.decodeIfPresent(Double.self, forKey: .idleSeconds) ?? d.idleSeconds
        retentionDays = try c.decodeIfPresent(Int.self, forKey: .retentionDays) ?? d.retentionDays
        maxWidth = try c.decodeIfPresent(Int.self, forKey: .maxWidth) ?? d.maxWidth
        ffmpeg = try c.decodeIfPresent(String.self, forKey: .ffmpeg) ?? d.ffmpeg
    }

    enum CodingKeys: String, CodingKey {
        case apps, browsers, braveProfiles, terminalApps, terminalTitleContains, intervalSeconds, idleSeconds,
             retentionDays, maxWidth, ffmpeg
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(apps, forKey: .apps); try c.encode(browsers, forKey: .browsers)
        try c.encode(terminalApps, forKey: .terminalApps); try c.encode(terminalTitleContains, forKey: .terminalTitleContains)
        try c.encode(intervalSeconds, forKey: .intervalSeconds); try c.encode(idleSeconds, forKey: .idleSeconds)
        try c.encode(retentionDays, forKey: .retentionDays); try c.encode(maxWidth, forKey: .maxWidth)
        try c.encode(ffmpeg, forKey: .ffmpeg)
    }

    var ffmpegPath: String {
        let candidates = [ffmpeg, "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"].filter { !$0.isEmpty }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/opt/homebrew/bin/ffmpeg"
    }

    var needsProfileDetection: Bool { browsers.values.contains { !$0.contains("*") && !$0.isEmpty } }
}

let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("WorkTape")
let framesDir = root.appendingPathComponent("frames")
let videosDir = root.appendingPathComponent("videos")
let bundleID = Bundle.main.bundleIdentifier ?? "com.worktape.app"

func loadConfig() -> Config {
    let url = root.appendingPathComponent("config.json")
    if let data = try? Data(contentsOf: url) {
        if let c = try? JSONDecoder().decode(Config.self, from: data) { return c }
        return Config()   // unreadable (e.g. a typo): run on defaults, never overwrite the user's file
    }
    let c = Config()
    let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
    try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    try? enc.encode(c).write(to: url)
    return c
}

func fmt(_ pattern: String) -> DateFormatter {
    let f = DateFormatter(); f.dateFormat = pattern; f.locale = Locale(identifier: "en_US_POSIX"); return f
}
let dayF = fmt("yyyy-MM-dd"), hourF = fmt("HH"), timeF = fmt("HHmmss"), isoF = fmt("yyyy-MM-dd HH:mm:ss")

final class WorkTape: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    var classifier: Process?
    var classifierStatus = ""
    var classifierDidWork = false
    var classifierManual = false
    var dashboard: NSWindow?
    var web: WKWebView?
    var config = loadConfig()
    var paused = false
    var busy = false
    var hasAccess = false
    // System-wide input counters (no permission needed). Deltas per tick separate typing from scrolling.
    var lastCounts: (keys: UInt32, scrolls: UInt32, clicks: UInt32)?
    var statusItem: NSStatusItem!
    let pauseItem = NSMenuItem(title: "Pause", action: #selector(togglePause), keyEquivalent: "")

    func applicationDidFinishLaunching(_ n: Notification) {
        // System Settings relaunches the app after a permission grant; keep only one copy.
        let me = NSRunningApplication.current
        if NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).contains(where: { $0 != me }) {
            exit(0)
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        pauseItem.target = self
        menu.addItem(pauseItem)
        let dash = NSMenuItem(title: "Open dashboard", action: #selector(openDashboard), keyEquivalent: "")
        dash.target = self
        menu.addItem(dash)
        let open = NSMenuItem(title: "Open recordings folder", action: #selector(openFolder), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit WorkTape", action: #selector(NSApp.terminate), keyEquivalent: ""))
        statusItem.menu = menu
        setIcon("○")

        // macOS reads the grant at launch only; ask once, then wait for a restart.
        hasAccess = CGPreflightScreenCaptureAccess()
        if !hasAccess { CGRequestScreenCaptureAccess(); setIcon("⚠︎") }
        if config.needsProfileDetection && !AXIsProcessTrusted() {   // only needed to tell browser profiles apart
            AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        }

        buildMainMenu()
        openDashboard()   // at every login, so the reports don't get forgotten

        Timer.scheduledTimer(withTimeInterval: config.intervalSeconds, repeats: true) { [weak self] _ in self?.tick() }
        Timer.scheduledTimer(withTimeInterval: 600, repeats: true) { _ in Self.housekeeping() }
        Self.housekeeping()
    }

    func setIcon(_ s: String) { statusItem.button?.title = s }

    @objc func togglePause() {
        paused.toggle()
        pauseItem.title = paused ? "Resume" : "Pause"
        setIcon(paused ? "⏸" : "○")
    }

    @objc func openFolder() { NSWorkspace.shared.open(root) }

    // MARK: dashboard window (the HTML reports, read straight from ~/WorkTape)

    @objc func openDashboard() {
        if dashboard == nil {
            let cfg = WKWebViewConfiguration()
            cfg.mediaTypesRequiringUserActionForPlayback = []
            cfg.userContentController.add(self, name: "worktape")   // the page's "Classify now" button
            let view = WKWebView(frame: .zero, configuration: cfg)
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 880),
                               styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            win.title = "WorkTape"
            win.contentView = view
            win.isReleasedWhenClosed = false
            win.delegate = self
            win.setFrameAutosaveName("WorkTapeDashboard")
            if !win.setFrameUsingName("WorkTapeDashboard") { win.center() }
            dashboard = win
            web = view
        }
        goHome()
        // Past 07:00 and something wasn't classified (laptop was off at 7)? Catch up quietly in the background.
        if Calendar.current.component(.hour, from: Date()) >= 7 { runClassifier(["--catch-up"], label: nil) }
        NSApp.setActivationPolicy(.regular)   // Dock icon + menu bar while the window is open
        dashboard?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func goHome() {
        let index = root.appendingPathComponent("reports/index.html")
        if !FileManager.default.fileExists(atPath: index.path) {
            try? FileManager.default.createDirectory(at: index.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? "<html><body style='font:15px -apple-system;padding:40px'><h2>WorkTape</h2><p>Recording has started. The first report is written at 07:00 tomorrow.</p><p style='color:#777'>Settings: ~/WorkTape/config.json (what gets recorded) and ~/WorkTape/classify.json (your name, what you do, your clients). Details in the README.</p></body></html>"
                .write(to: index, atomically: true, encoding: .utf8)
        }
        web?.loadFileURL(index, allowingReadAccessTo: root)
    }

    // MARK: classifier on demand

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        switch message.body as? String {
        case "classify": runClassifier(["--now"], label: "Starting…")
        case "hello": pushStatus()   // a page (re)loaded while a run is going
        default: break
        }
    }

    func runClassifier(_ args: [String], label: String?) {
        guard classifier == nil else { pushStatus(); return }
        let script = root.appendingPathComponent("bin/classify.py").path
        guard FileManager.default.fileExists(atPath: script) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: FileManager.default.isExecutableFile(atPath: "/usr/bin/python3")
                              ? "/usr/bin/python3" : "/opt/homebrew/bin/python3")
        p.arguments = [script] + args
        var env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        let logURL = root.appendingPathComponent("logs/manual.log")
        try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
        let logHandle = try? FileHandle(forWritingTo: logURL)
        logHandle?.seekToEndOfFile()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            logHandle?.write(d)
            let text = String(decoding: d, as: UTF8.self)
            if text.contains(": done,") { DispatchQueue.main.async { self?.classifierDidWork = true } }
            // show the latest progress line, without the "[HH:MM:SS]" prefix
            if let last = text.split(separator: "\n").last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }) {
                let line = last.replacingOccurrences(of: #"^\[\d\d:\d\d:\d\d\] "#, with: "", options: .regularExpression)
                DispatchQueue.main.async { self?.classifierStatus = line; self?.pushStatus() }
            }
        }
        p.terminationHandler = { [weak self] _ in
            pipe.fileHandleForReading.readabilityHandler = nil
            try? logHandle?.close()
            DispatchQueue.main.async {
                guard let self else { return }
                self.classifier = nil
                self.classifierStatus = ""
                if self.classifierDidWork || self.classifierManual { self.web?.reload() } else { self.pushStatus() }
            }
        }
        do {
            try p.run()
            classifier = p
            classifierDidWork = false
            classifierManual = label != nil
            classifierStatus = label ?? ""
            pushStatus()
        } catch {
            classifierStatus = "Could not start the classifier: \(error.localizedDescription)"
            pushStatus()
        }
    }

    func pushStatus() {
        let busy = classifier != nil
        let data = try? JSONSerialization.data(withJSONObject: [classifierStatus])
        let arg = data.flatMap { String(data: $0, encoding: .utf8) }.map { String($0.dropFirst().dropLast()) } ?? "\"\""
        web?.evaluateJavaScript("window.worktapeStatus && window.worktapeStatus(\(arg), \(busy))")
    }

    @objc func goBack() { web?.goBack() }
    @objc func reload() { web?.reload() }

    func windowWillClose(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)  // back to menu-bar only
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openDashboard()
        return true
    }

    func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        appMenu.addItem(NSMenuItem(title: "Quit WorkTape", action: #selector(NSApp.terminate), keyEquivalent: "q"))
        appItem.submenu = appMenu
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        for (title, sel, key) in [("Home", #selector(goHome), "h"), ("Back", #selector(goBack), "["), ("Reload", #selector(reload), "r")] {
            let it = NSMenuItem(title: title, action: sel, keyEquivalent: key)
            it.target = self
            viewMenu.addItem(it)
        }
        viewItem.submenu = viewMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = editMenu
        NSApp.mainMenu = main
    }

    // MARK: decide

    func frontWindow(pid: pid_t) -> (title: String, bounds: CGRect)? {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
        // Chromium stacks untitled helper windows above the real one, so take the first titled window.
        var fallback: (title: String, bounds: CGRect)?
        for w in list where (w[kCGWindowOwnerPID as String] as? pid_t) == pid && (w[kCGWindowLayer as String] as? Int) == 0 {
            var rect = CGRect.zero
            if let b = w[kCGWindowBounds as String] { rect = CGRect(dictionaryRepresentation: b as! CFDictionary) ?? .zero }
            let name = (w[kCGWindowName as String] as? String) ?? ""
            if !name.isEmpty { return (name, rect) }
            if fallback == nil { fallback = (name, rect) }
        }
        return fallback
    }

    func shouldRecord(bundle: String, title: String) -> Bool {
        if config.apps.contains(bundle) { return true }
        if let profiles = config.browsers[bundle], !profiles.isEmpty {
            if profiles.contains("*") { return true }
            guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else { return false }
            let labels = toolbarLabels(pid: pid).map { $0.lowercased() }
            return profiles.contains { labels.contains($0.lowercased()) }
        }
        if config.terminalApps.contains(bundle) {
            let t = title.lowercased()
            return config.terminalTitleContains.contains { t.contains($0.lowercased()) }
        }
        return false
    }

    // Chromium window titles carry no profile name, so read the toolbar's profile button via Accessibility.
    // Cached per window: a window's profile never changes.
    var axCache: (window: AXUIElement, labels: [String])?
    var axEnabledPids = Set<pid_t>()

    func axAttr(_ el: AXUIElement, _ name: String) -> AnyObject? {
        var v: AnyObject?
        return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
    }

    func toolbarLabels(pid: pid_t) -> [String] {
        guard AXIsProcessTrusted() else { return [] }
        let app = AXUIElementCreateApplication(pid)
        if !axEnabledPids.contains(pid) {   // Chromium builds its AX tree only when asked
            AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
            axEnabledPids.insert(pid)
        }
        guard let winRef = axAttr(app, kAXFocusedWindowAttribute) else { return [] }
        let window = winRef as! AXUIElement
        if let c = axCache, CFEqual(c.window, window), !c.labels.isEmpty { return c.labels }

        var labels: [String] = []
        var queue: [(AXUIElement, Bool)] = [(window, false)]
        var visited = 0
        while !queue.isEmpty && visited < 2000 {
            let (el, inToolbar) = queue.removeFirst()
            visited += 1
            let role = axAttr(el, kAXRoleAttribute) as? String ?? ""
            if role == "AXWebArea" { continue }   // skip page content
            let toolbar = inToolbar || role == kAXToolbarRole
            if toolbar && role == kAXButtonRole || toolbar && role == kAXPopUpButtonRole {
                for a in [kAXDescriptionAttribute, kAXTitleAttribute, kAXHelpAttribute] {
                    if let s = axAttr(el, a) as? String, !s.isEmpty { labels.append(s) }
                }
            }
            for child in (axAttr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? [] { queue.append((child, toolbar)) }
        }
        axCache = (window, labels)
        return labels
    }

    func inputDeltas() -> (keys: Int, scrolls: Int, clicks: Int) {
        func c(_ t: CGEventType) -> UInt32 { CGEventSource.counterForEventType(.combinedSessionState, eventType: t) }
        let now = (keys: c(.keyDown), scrolls: c(.scrollWheel), clicks: c(.leftMouseDown) &+ c(.rightMouseDown))
        defer { lastCounts = now }
        guard let last = lastCounts else { return (0, 0, 0) }
        return (Int(now.keys &- last.keys), Int(now.scrolls &- last.scrolls), Int(now.clicks &- last.clicks))
    }

    func secondsSinceInput() -> Double {
        CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!)
    }

    func idle() -> Bool {
        if secondsSinceInput() > config.idleSeconds { return true }
        if let d = CGSessionCopyCurrentDictionary() as? [String: Any], d["CGSSessionScreenIsLocked"] as? Bool == true { return true }
        return false
    }

    func tick() {
        let deltas = inputDeltas()   // every tick, so a skipped capture doesn't pile counts onto the next one
        guard !busy, let app = NSWorkspace.shared.frontmostApplication, let bundle = app.bundleIdentifier else { return }
        let win = frontWindow(pid: app.processIdentifier)
        let title = win?.title ?? ""
        // Without permission, every capture attempt re-triggers the macOS prompt, so don't try.
        guard hasAccess else { writeStatus("\(isoF.string(from: Date()))\tNO-ACCESS\t\(bundle)\t\(title)"); return }
        let recording = !paused && !idle() && shouldRecord(bundle: bundle, title: title)
        writeStatus("\(isoF.string(from: Date()))\t\(recording ? "REC" : "---")\t\(bundle)\t\(title)")
        if FileManager.default.fileExists(atPath: root.appendingPathComponent("debug").path) { writeDebug(pid: app.processIdentifier) }
        if !paused { setIcon(recording ? "●" : "○") }
        guard recording else { return }

        busy = true
        let center = CGPoint(x: win?.bounds.midX ?? 0, y: win?.bounds.midY ?? 0)
        Task {
            defer { DispatchQueue.main.async { self.busy = false } }
            await self.capture(near: center, bundle: bundle, title: title, inputAge: self.secondsSinceInput(), deltas: deltas)
        }
    }

    func writeStatus(_ line: String) {
        try? (line + "\n").write(to: root.appendingPathComponent("status.txt"), atomically: true, encoding: .utf8)
    }

    // Touch ~/WorkTape/debug to dump every window of the front app to debug.txt.
    func writeDebug(pid: pid_t) {
        var out = "screenCaptureAccess=\(CGPreflightScreenCaptureAccess()) accessibility=\(AXIsProcessTrusted())\n"
        if let b = NSWorkspace.shared.frontmostApplication?.bundleIdentifier, config.browsers[b] != nil {
            out += "browserToolbar=\(toolbarLabels(pid: pid))\n"
        }
        let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        for w in list where (w[kCGWindowOwnerPID as String] as? pid_t) == pid {
            out += "layer=\(w[kCGWindowLayer as String] ?? "?")\tname=\(w[kCGWindowName as String] ?? "<nil>")\tbounds=\(w[kCGWindowBounds as String] ?? "")\n"
        }
        try? out.write(to: root.appendingPathComponent("debug.txt"), atomically: true, encoding: .utf8)
    }

    // MARK: capture

    func capture(near point: CGPoint, bundle: String, title: String, inputAge: Double,
                 deltas: (keys: Int, scrolls: Int, clicks: Int)) async {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first(where: { $0.frame.contains(point) }) ?? content.displays.first else { return }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let cfg = SCStreamConfiguration()
            let scale = min(1.0, Double(config.maxWidth) / Double(display.width))
            cfg.width = Int(Double(display.width) * scale)
            cfg.height = Int(Double(display.height) * scale)
            cfg.showsCursor = true
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg)

            let now = Date()
            let dir = framesDir.appendingPathComponent(dayF.string(from: now)).appendingPathComponent(hourF.string(from: now))
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let rep = NSBitmapImageRep(cgImage: image)
            guard let jpg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.6]) else { return }
            try jpg.write(to: dir.appendingPathComponent(timeF.string(from: now) + ".jpg"))

            let log = dir.appendingPathComponent("log.tsv")
            // columns 4-7: seconds since last input, then keystrokes, scroll ticks and clicks since the previous tick
            let line = "\(isoF.string(from: now))\t\(bundle)\t\(title.replacingOccurrences(of: "\t", with: " "))\t\(Int(inputAge))\t\(deltas.keys)\t\(deltas.scrolls)\t\(deltas.clicks)\n"
            if let h = try? FileHandle(forWritingTo: log) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close() }
            else { try line.write(to: log, atomically: true, encoding: .utf8) }
        } catch {
            writeStatus("\(isoF.string(from: Date()))\tERROR\t\(error.localizedDescription)")
        }
    }

    // MARK: stitch finished hours into video, drop old days

    static let housekeepingQueue = DispatchQueue(label: "worktape.housekeeping")

    static func housekeeping() {
        housekeepingQueue.async {
            let config = loadConfig()
            let fm = FileManager.default
            let now = Date()
            let currentHour = "\(dayF.string(from: now))/\(hourF.string(from: now))"

            for day in (try? fm.contentsOfDirectory(atPath: framesDir.path)) ?? [] where !day.hasPrefix(".") {
                let dayDir = framesDir.appendingPathComponent(day)
                for hour in (try? fm.contentsOfDirectory(atPath: dayDir.path)) ?? [] where !hour.hasPrefix(".") {
                    guard "\(day)/\(hour)" != currentHour else { continue }
                    stitch(frames: dayDir.appendingPathComponent(hour), day: day, hour: hour, config: config)
                }
                if ((try? fm.contentsOfDirectory(atPath: dayDir.path)) ?? []).filter({ !$0.hasPrefix(".") }).isEmpty {
                    try? fm.removeItem(at: dayDir)
                }
            }

            let cutoff = Calendar.current.date(byAdding: .day, value: -config.retentionDays, to: now)!
            for day in (try? fm.contentsOfDirectory(atPath: videosDir.path)) ?? [] {
                if let d = dayF.date(from: day), d < cutoff { try? fm.removeItem(at: videosDir.appendingPathComponent(day)) }
            }
        }
    }

    static func stitch(frames: URL, day: String, hour: String, config: Config) {
        let fm = FileManager.default
        let jpgs = ((try? fm.contentsOfDirectory(atPath: frames.path)) ?? []).filter { $0.hasSuffix(".jpg") }
        let outDir = videosDir.appendingPathComponent(day)
        try? fm.createDirectory(at: outDir, withIntermediateDirectories: true)
        let log = frames.appendingPathComponent("log.tsv")

        if !jpgs.isEmpty {
            let out = outDir.appendingPathComponent("\(hour).mp4")
            let w = config.maxWidth, h = config.maxWidth * 10 / 16
            let p = Process()
            p.executableURL = URL(fileURLWithPath: config.ffmpegPath)
            p.arguments = [
                "-y", "-loglevel", "error",
                "-framerate", String(1.0 / config.intervalSeconds),
                "-pattern_type", "glob", "-i", frames.path + "/*.jpg",
                "-vf", "scale=\(w):\(h):force_original_aspect_ratio=decrease,pad=\(w):\(h):(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                "-c:v", "hevc_videotoolbox", "-q:v", "50", "-tag:v", "hvc1",
                out.path,
            ]
            do { try p.run(); p.waitUntilExit() } catch { return }
            guard p.terminationStatus == 0 else { return }   // keep frames if encoding failed
        }
        if fm.fileExists(atPath: log.path) {
            let dest = outDir.appendingPathComponent("\(hour).tsv")
            try? fm.removeItem(at: dest)
            try? fm.moveItem(at: log, to: dest)
        }
        try? fm.removeItem(at: frames)
    }
}

let app = NSApplication.shared
let delegate = WorkTape()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
