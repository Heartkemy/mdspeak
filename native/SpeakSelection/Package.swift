// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "SpeakSelection",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "SpeakSelection",
            path: "Sources/SpeakSelection"
        )
    ]
)
