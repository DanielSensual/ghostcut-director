// lips_helper: inner-lip opening per frame for `director sync --methods lips`.
//
// Usage: lips_helper <frames-dir>
// Reads the JPEGs in <frames-dir> in sorted order and prints one line per
// frame on stdout: "<index> <opening> <x>,<y>,<w>,<h>", where <opening> is
// the inner-lip height (max - min y of innerLips.normalizedPoints) on the
// largest face and the box is the Vision face bounding box in normalised
// coordinates. A frame with no usable face prints "<index> NOFACE"; a face
// whose inner lips were not detected prints "NA" for the opening but still
// prints the box. Nothing is printed on stdout except these lines; errors
// go to stderr and the exit status is 0 unless <frames-dir> is unreadable.
//
// Built by sync.py on first use with `swiftc -O` into the ghostcut-director
// cache directory, keyed by this file's hash. macOS only (Apple Vision).

import CoreGraphics
import Foundation
import ImageIO
import Vision

func lineForImage(at url: URL) -> String {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        return "NOFACE"
    }
    let request = VNDetectFaceLandmarksRequest()
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return "NOFACE"
    }
    let faces = request.results ?? []
    let face = faces.max(by: {
        $0.boundingBox.width * $0.boundingBox.height
            < $1.boundingBox.width * $1.boundingBox.height
    })
    guard let face = face else {
        return "NOFACE"
    }
    let box = face.boundingBox
    var opening = "NA"
    if let lips = face.landmarks?.innerLips, !lips.normalizedPoints.isEmpty {
        let ys = lips.normalizedPoints.map { Double($0.y) }
        if let lo = ys.min(), let hi = ys.max(), hi >= lo {
            opening = String(format: "%.6f", hi - lo)
        }
    }
    return String(
        format: "%@ %.4f,%.4f,%.4f,%.4f", opening,
        box.origin.x, box.origin.y, box.size.width, box.size.height)
}

let args = CommandLine.arguments
guard args.count == 2 else {
    fputs("usage: lips_helper <frames-dir>\n", stderr)
    exit(2)
}
let dirURL = URL(fileURLWithPath: args[1], isDirectory: true)
let files: [URL]
do {
    files = try FileManager.default.contentsOfDirectory(
        at: dirURL, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
    )
    .filter {
        let ext = $0.pathExtension.lowercased()
        return ext == "jpg" || ext == "jpeg"
    }
    .sorted(by: { $0.lastPathComponent < $1.lastPathComponent })
} catch {
    fputs("lips_helper: cannot read \(args[1])\n", stderr)
    exit(1)
}
for (index, url) in files.enumerated() {
    print("\(index) \(lineForImage(at: url))")
}
