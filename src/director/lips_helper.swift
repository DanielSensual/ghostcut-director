// lips_helper: inner-lip opening and face pose per frame for `director sync
// --methods lips`, the `angles` gaze rule and the `grade` skin match.
//
// Usage: lips_helper <frames-dir>
// Reads the JPEGs in <frames-dir> in sorted order and prints one line per
// frame on stdout:
// "<index> <opening> <x>,<y>,<w>,<h> <yaw>,<pitch>,<roll> <yaw_continuous>",
// where <opening> is the inner-lip height (max - min y of
// innerLips.normalizedPoints) on the largest face, the box is the Vision
// face bounding box in normalised coordinates, and the pose is Vision's
// yaw, pitch and roll for that face in degrees (each "NA" when Vision did
// not report it). Vision's yaw is quantised (0, +/-45, ...), so
// <yaw_continuous> is a continuous estimate in degrees from the face
// landmarks: the nose tip's sideways offset from the midpoint between
// the eye centres, converted with atan2(offset, 0.55 * eyeDistance)
// (the nose tip sits roughly 0.55 inter-eye distances in front of the
// eye plane) and clamped to +/-75 degrees, its sign aligned with the
// coarse yaw when that is non-zero. It prints "NA" when the eye or nose
// landmarks are missing. A frame with no usable face prints
// "<index> NOFACE"; a face whose inner lips were not detected prints
// "NA" for the opening but still prints the box and the pose. Nothing is
// printed on stdout except these lines; errors go to stderr and the
// exit status is 0 unless <frames-dir> is unreadable.
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
    func deg(_ radians: Double?) -> String {
        guard let radians = radians else { return "NA" }
        return String(format: "%.2f", radians * 180.0 / Double.pi)
    }
    let pose = "\(deg(face.yaw?.doubleValue)),\(deg(face.pitch?.doubleValue)),\(deg(face.roll?.doubleValue))"
    var continuous = "NA"
    if let landmarks = face.landmarks,
        let leftEye = landmarks.leftEye, !leftEye.normalizedPoints.isEmpty,
        let rightEye = landmarks.rightEye, !rightEye.normalizedPoints.isEmpty,
        let nose = landmarks.nose, !nose.normalizedPoints.isEmpty
    {
        func centroid(_ points: [CGPoint]) -> CGPoint {
            let n = Double(points.count)
            return CGPoint(
                x: points.reduce(0.0) { $0 + Double($1.x) } / n,
                y: points.reduce(0.0) { $0 + Double($1.y) } / n)
        }
        let leftCentre = centroid(leftEye.normalizedPoints)
        let rightCentre = centroid(rightEye.normalizedPoints)
        // Landmark points are normalised to the face box with the origin
        // at its lower left, so the nose tip is the lowest nose point.
        let noseTip = nose.normalizedPoints.min(by: { $0.y < $1.y }) ?? nose.normalizedPoints[0]
        let eyeMidX = (Double(leftCentre.x) + Double(rightCentre.x)) / 2.0
        let eyeDistance = hypot(
            Double(rightCentre.x - leftCentre.x),
            Double(rightCentre.y - leftCentre.y))
        if eyeDistance > 0.01 {
            var yawDeg = atan2(
                Double(noseTip.x) - eyeMidX, 0.55 * eyeDistance)
                * 180.0 / Double.pi
            yawDeg = max(-75.0, min(75.0, yawDeg))
            // The landmark x direction need not agree with Vision's yaw
            // sign; align with the coarse yaw when Vision reports one.
            if let coarse = face.yaw?.doubleValue {
                let coarseDeg = coarse * 180.0 / Double.pi
                if coarseDeg != 0, yawDeg != 0,
                    (coarseDeg < 0) != (yawDeg < 0)
                {
                    yawDeg = -yawDeg
                }
            }
            continuous = String(format: "%.2f", yawDeg)
        }
    }
    return String(
        format: "%@ %.4f,%.4f,%.4f,%.4f %@ %@", opening,
        box.origin.x, box.origin.y, box.size.width, box.size.height, pose,
        continuous)
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
