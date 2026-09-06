import AppKit

// Menu-bar only: no Dock icon, no window. The status item is the whole UI.
let delegate = AppDelegate()
NSApplication.shared.setActivationPolicy(.accessory)
NSApplication.shared.delegate = delegate
NSApplication.shared.run()
