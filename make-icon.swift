// Draws the WorkTape app icon (1024x1024 PNG): a cassette-tape window on a dark squircle with a red record dot.
// Usage: swift make-icon.swift out.png
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    CGColor(red: r / 255, green: g / 255, blue: b / 255, alpha: a)
}

// Background squircle (macOS icon grid: 824pt body inside 1024 canvas)
let body = CGRect(x: 100, y: 100, width: 824, height: 824)
let squircle = CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: rgb(0, 0, 0, 0.35))
ctx.addPath(squircle); ctx.setFillColor(rgb(28, 28, 30)); ctx.fillPath()
ctx.restoreGState()
ctx.saveGState()
ctx.addPath(squircle); ctx.clip()
let bg = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: [rgb(52, 50, 48), rgb(22, 21, 20)] as CFArray,
                    locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])
ctx.restoreGState()

// Cassette window
let window = CGRect(x: 212, y: 345, width: 600, height: 300)
ctx.addPath(CGPath(roundedRect: window, cornerWidth: 70, cornerHeight: 70, transform: nil))
ctx.setFillColor(rgb(245, 240, 232)); ctx.fillPath()

// Tape running between the reels
ctx.setFillColor(rgb(120, 86, 60))
ctx.fill(CGRect(x: 360, y: 452, width: 304, height: 30))

// Reels
for cx in [360.0, 664.0] as [CGFloat] {
    let c = CGPoint(x: cx, y: 495)
    ctx.setFillColor(rgb(120, 86, 60))                                       // wound tape
    ctx.fillEllipse(in: CGRect(x: c.x - 104, y: c.y - 104, width: 208, height: 208))
    ctx.setFillColor(rgb(245, 240, 232))                                     // hub ring
    ctx.fillEllipse(in: CGRect(x: c.x - 62, y: c.y - 62, width: 124, height: 124))
    ctx.setFillColor(rgb(28, 28, 30))                                        // spindle
    ctx.fillEllipse(in: CGRect(x: c.x - 26, y: c.y - 26, width: 52, height: 52))
    ctx.setFillColor(rgb(245, 240, 232))                                     // spindle teeth
    for k in 0..<3 {
        let a = CGFloat(k) * 2 * .pi / 3 + .pi / 2
        let p = CGPoint(x: c.x + cos(a) * 20, y: c.y + sin(a) * 20)
        ctx.fillEllipse(in: CGRect(x: p.x - 9, y: p.y - 9, width: 18, height: 18))
    }
}

// Red record dot, top right
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 40, color: rgb(255, 59, 48, 0.7))
ctx.setFillColor(rgb(255, 59, 48))
ctx.fillEllipse(in: CGRect(x: 690, y: 718, width: 120, height: 120))
ctx.restoreGState()

NSGraphicsContext.current = nil
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
