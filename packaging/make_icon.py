"""Renders packaging/touch-seq.png (256x256): a little rack module with screws and a lit step grid.
Usage: QT_QPA_PLATFORM=offscreen python packaging/make_icon.py"""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen, QRadialGradient

app = QGuiApplication([])
size = 256
img = QImage(size, size, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)

plate = QRectF(10, 10, size - 20, size - 20)
metal = QLinearGradient(plate.topLeft(), plate.bottomRight())
metal.setColorAt(0, QColor("#4a505a"))
metal.setColorAt(1, QColor("#262a31"))
p.setPen(QPen(QColor("#6a717d"), 2))
p.setBrush(metal)
p.drawRoundedRect(plate, 30, 30)

for x, y in ((40, 40), (size - 40, 40), (40, size - 40), (size - 40, size - 40)):
    c = QPointF(x, y)
    g = QRadialGradient(c - QPointF(3, 3), 12)
    g.setColorAt(0, QColor("#f1f3f6"))
    g.setColorAt(0.6, QColor("#a7adb7"))
    g.setColorAt(1, QColor("#5a606a"))
    p.setPen(QPen(QColor("#1b1e22"), 1.5))
    p.setBrush(g)
    p.drawEllipse(c, 10, 10)
    p.setPen(QPen(QColor("#3b4048"), 3, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(c - QPointF(6, 3), c + QPointF(6, 3))

screen = QRectF(58, 58, size - 116, size - 116)
p.setPen(QPen(QColor("#121418"), 5))
p.setBrush(QColor("#0d0f14"))
p.drawRoundedRect(screen, 12, 12)
colors = ["#38d6ff", "#ff8c42", "#a3e635", "#f472b6"]
lit = {(0, 0), (0, 2), (1, 1), (1, 3), (2, 0), (2, 1), (3, 2), (3, 3)}
cell = (screen.width() - 30) / 4
for row in range(4):
    for col in range(4):
        r = QRectF(screen.left() + 10 + col * (cell + 3.3), screen.top() + 10 + row * (cell + 3.3), cell, cell)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(colors[row]) if (row, col) in lit else QColor("#1d2230"))
        p.drawRoundedRect(r, 5, 5)
p.end()
out = Path(__file__).with_name("touch-seq.png")
img.save(str(out))
print("wrote", out)
