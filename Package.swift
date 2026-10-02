// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "AudioWhisper",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        .package(url: "https://github.com/soffes/HotKey", from: "0.2.1")
    ],
    targets: [
        .executableTarget(
            name: "AudioWhisper",
            dependencies: ["HotKey"],
            path: "Sources",
            exclude: ["VersionInfo.swift.template"],
            resources: [
                .process("Assets.xcassets"),
                .copy("ml_daemon.py"),
                .copy("ml"),
                // Bundle additional resources like uv binary and lock files
                .copy("Resources")
            ]
        ),
        .testTarget(
            name: "AudioWhisperTests",
            dependencies: ["AudioWhisper"],
            path: "Tests",
            exclude: ["README.md", "test_ml_rpc.py", "test_ml_setup.py", "test_ml_loader.py", "test_stt_server.py", "__pycache__", "__Snapshots__"]
        )
    ]
)
