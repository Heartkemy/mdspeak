import AppKit
import ApplicationServices
import CoreGraphics

/// Reads the selected text of the frontmost app, with a clipboard fallback for
/// apps that don't expose their selection (secure fields, password managers,
/// terminals).
///
/// Strategy, in order:
/// 1. Accessibility API — `kAXSelectedTextAttribute` on the focused element and
///    its ancestors, then the focused window, then a bounded search of the whole
///    tree (Chromium/Electron sometimes hang the selection on a non-focused
///    node, as text or a `kAXSelectedTextRangeAttribute` range).
/// 2. Copy trick — simulate ⌘C, read the clipboard, restore it. Electron apps
///    (T3 Code, VS Code, Obsidian) don't reliably populate their AX tree until
///    an assistive tech has connected, so this is what makes them work.
enum SelectionReader {
    private static let terminalBundleIDs: Set<String> = [
        "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable",
        "org.alacritty", "net.kovidgoyal.kitty", "org.wezfurlong.wezterm",
        "co.zeit.hyper", "io.alacritty",
    ]

    static func selectedText() -> String? {
        guard let app = NSWorkspace.shared.frontmostApplication else { return nil }

        if let text = axSelectedText(app: app) {
            return text
        }
        if let text = copyTrick(app: app) {
            return text
        }
        return nil
    }

    static func clipboardText() -> String? {
        NSPasteboard.general.string(forType: .string)
    }

    // ---------------------------------------------------------------- AX

    private static func axSelectedText(app: NSRunningApplication) -> String? {
        let name = app.localizedName ?? "?"
        let axApp = AXUIElementCreateApplication(app.processIdentifier)

        if let focused = focusedElement(of: axApp),
           let text = text(from: focused, depth: 12) {
            return text
        }

        if let windowValue = attribute(axApp, kAXFocusedWindowAttribute as CFString) {
            let window = windowValue as! AXUIElement
            if let focused = focusedElement(of: window),
               let text = text(from: focused, depth: 12) {
                return text
            }
            if let text = searchTree(window, depth: 12) {
                return text
            }
        }

        if let text = searchTree(axApp, depth: 12) {
            return text
        }

        NSLog("SpeakSelection: AX found no selection in %@; trying copy trick", name)
        return nil
    }

    /// Walk from `element` up through its ancestors.
    private static func text(from element: AXUIElement, depth: Int) -> String? {
        var current: AXUIElement? = element
        var remaining = depth
        while let el = current, remaining > 0 {
            if let text = focusedSelection(el) {
                return text
            }
            current = parent(el)
            remaining -= 1
        }
        return nil
    }

    /// Depth-first search of the whole subtree, bounded so it can't stall.
    private static func searchTree(_ element: AXUIElement, depth: Int) -> String? {
        var budget = 2000
        func walk(_ el: AXUIElement, _ remaining: Int) -> String? {
            guard remaining >= 0, budget > 0 else { return nil }
            budget -= 1
            if let text = treeSelection(el) {
                return text
            }
            guard let children = attribute(el, kAXChildrenAttribute as CFString) as? [AXUIElement] else {
                return nil
            }
            for child in children {
                if let text = walk(child, remaining - 1) {
                    return text
                }
            }
            return nil
        }
        return walk(element, depth)
    }

    /// Selection on the focused element — the value fallback is reasonable here.
    private static func focusedSelection(_ element: AXUIElement) -> String? {
        selectionOn(element, includeValue: true)
    }

    /// Selection in the tree — no value fallback, or we'd grab a text field's
    /// full contents instead of the real selection.
    private static func treeSelection(_ element: AXUIElement) -> String? {
        selectionOn(element, includeValue: false)
    }

    private static func selectionOn(_ element: AXUIElement, includeValue: Bool) -> String? {
        if let text = nonEmpty(stringAttribute(element, kAXSelectedTextAttribute as CFString)) {
            return text
        }
        if includeValue, let text = nonEmpty(stringAttribute(element, kAXValueAttribute as CFString)) {
            return text
        }
        return nonEmpty(selectedTextViaRange(element))
    }

    private static func focusedElement(of element: AXUIElement) -> AXUIElement? {
        guard let value = attribute(element, kAXFocusedUIElementAttribute as CFString) else {
            return nil
        }
        return (value as! AXUIElement)
    }

    private static func parent(_ element: AXUIElement) -> AXUIElement? {
        guard let value = attribute(element, kAXParentAttribute as CFString) else { return nil }
        return (value as! AXUIElement)
    }

    private static func stringAttribute(_ element: AXUIElement, _ attribute: CFString) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
              let value else { return nil }
        return value as? String
    }

    /// Chromium exposes a selection *range*; resolve it to a string.
    private static func selectedTextViaRange(_ element: AXUIElement) -> String? {
        var range: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            element, kAXSelectedTextRangeAttribute as CFString, &range
        ) == .success, let range else { return nil }
        var text: CFTypeRef?
        guard AXUIElementCopyParameterizedAttributeValue(
            element, kAXStringForRangeParameterizedAttribute as CFString, range, &text
        ) == .success, let text else { return nil }
        return text as? String
    }

    private static func attribute(_ element: AXUIElement, _ attribute: CFString) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
              let value else { return nil }
        return value
    }

    private static func nonEmpty(_ text: String?) -> String? {
        guard let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        return text
    }

    // ---------------------------------------------------------------- copy trick

    /// Simulate ⌘C and read the result, restoring the clipboard afterwards.
    /// Skipped for terminals, where ⌘C is SIGINT, not copy.
    private static func copyTrick(app: NSRunningApplication) -> String? {
        if let bid = app.bundleIdentifier, terminalBundleIDs.contains(bid) {
            return nil
        }

        let pb = NSPasteboard.general
        let snapshot = capturePasteboard()
        let beforeCount = pb.changeCount

        let source = CGEventSource(stateID: .hidSystemState)
        let keyC: CGKeyCode = 8 // kVK_ANSI_C
        let down = CGEvent(keyboardEventSource: source, virtualKey: keyC, keyDown: true)
        down?.flags = .maskCommand
        let up = CGEvent(keyboardEventSource: source, virtualKey: keyC, keyDown: false)
        up?.flags = .maskCommand
        down?.post(tap: .cghidEventTap)
        usleep(60_000)
        up?.post(tap: .cghidEventTap)

        // Wait for the clipboard to actually change (⌘C on nothing leaves it).
        let deadline = Date().addingTimeInterval(0.3)
        while Date() < deadline, pb.changeCount == beforeCount {
            usleep(20_000)
        }

        let copied = pb.string(forType: .string)

        // Restore the clipboard only if we changed it.
        if pb.changeCount != beforeCount {
            restorePasteboard(snapshot)
        }

        return nonEmpty(copied)
    }

    /// Snapshot every item and every type on the clipboard, so a restore can
    /// put back rich content (files, images, RTF, …) — not just the string.
    private static func capturePasteboard() -> [[NSPasteboard.PasteboardType: Data]] {
        let pb = NSPasteboard.general
        return (pb.pasteboardItems ?? []).map { item in
            var map: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types {
                if let data = item.data(forType: type) {
                    map[type] = data
                }
            }
            return map
        }
    }

    private static func restorePasteboard(_ snapshot: [[NSPasteboard.PasteboardType: Data]]) {
        let pb = NSPasteboard.general
        pb.clearContents()
        let items = snapshot.map { map -> NSPasteboardItem in
            let item = NSPasteboardItem()
            for (type, data) in map {
                item.setData(data, forType: type)
            }
            return item
        }
        pb.writeObjects(items)
    }

    // ---------------------------------------------------------------- diagnostics

    /// A bounded dump of the frontmost app's AX tree, marking nodes that carry
    /// a selection, for pasting back when an app refuses to yield its text.
    static func diagnostics() -> String {
        var lines: [String] = []
        var count = 0
        lines.append("AXIsProcessTrusted: \(AXIsProcessTrusted())")
        guard let app = NSWorkspace.shared.frontmostApplication else {
            lines.append("frontmost app: nil")
            return lines.joined(separator: "\n")
        }
        lines.append("frontmost: \(app.localizedName ?? "?") (pid \(app.processIdentifier))")
        let axApp = AXUIElementCreateApplication(app.processIdentifier)

        func walk(_ element: AXUIElement, _ indent: String, _ depth: Int) {
            guard count < 400, depth >= 0 else { return }
            count += 1
            let role = stringAttribute(element, kAXRoleAttribute as CFString) ?? "?"
            let sub = stringAttribute(element, kAXSubroleAttribute as CFString) ?? ""
            let desc = stringAttribute(element, kAXRoleDescriptionAttribute as CFString) ?? ""
            let sel = stringAttribute(element, kAXSelectedTextAttribute as CFString)
            let val = stringAttribute(element, kAXValueAttribute as CFString)
            var range: CFTypeRef?
            let rangeErr = AXUIElementCopyAttributeValue(
                element, kAXSelectedTextRangeAttribute as CFString, &range
            )
            var marks: [String] = []
            if let sel, !sel.isEmpty { marks.append("SEL[\(sel.count)c]") }
            if let val, !val.isEmpty { marks.append("VAL[\(val.count)c]") }
            if rangeErr == .success { marks.append("RANGE") }
            let subStr = sub.isEmpty ? "" : "/\(sub)"
            let descStr = desc.isEmpty ? "" : " \"\(desc)\""
            let marksStr = marks.isEmpty ? "" : "  <" + marks.joined(separator: " ") + ">"
            lines.append("\(indent)\(role)\(subStr)\(descStr)\(marksStr)")

            guard let children = attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] else {
                return
            }
            for child in children.prefix(12) {
                walk(child, indent + "  ", depth - 1)
            }
        }
        walk(axApp, "", 6)
        return lines.joined(separator: "\n")
    }
}
