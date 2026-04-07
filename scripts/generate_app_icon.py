from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)


ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "assets"
PNG_PATH = ASSETS_DIR / "crimson_texture_forge.png"
ICO_PATH = ASSETS_DIR / "crimson_texture_forge.ico"


def draw_icon(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)

    outer = QRectF(size * 0.08, size * 0.08, size * 0.84, size * 0.84)
    shadow = QRectF(outer)
    shadow.translate(size * 0.018, size * 0.024)

    shadow_grad = QRadialGradient(shadow.center() + QPointF(0, size * 0.02), shadow.width() * 0.7)
    shadow_grad.setColorAt(0.0, QColor(0, 0, 0, 120))
    shadow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(shadow_grad))
    painter.drawRoundedRect(shadow, size * 0.12, size * 0.12)

    bg_grad = QLinearGradient(outer.topLeft(), outer.bottomRight())
    bg_grad.setColorAt(0.0, QColor("#171b22"))
    bg_grad.setColorAt(1.0, QColor("#252a33"))
    painter.setBrush(QBrush(bg_grad))
    painter.drawRoundedRect(outer, size * 0.12, size * 0.12)

    back_plate = QRectF(size * 0.22, size * 0.29, size * 0.38, size * 0.38)
    back_plate.translate(-size * 0.04, size * 0.05)
    mid_plate = QRectF(size * 0.22, size * 0.29, size * 0.38, size * 0.38)
    mid_plate.translate(size * 0.01, size * 0.025)
    top_plate = QRectF(size * 0.22, size * 0.29, size * 0.38, size * 0.38)
    top_plate.translate(size * 0.06, -size * 0.005)

    for rect, colors in (
        (back_plate, ("#5a0e18", "#7a1823")),
        (mid_plate, ("#8c1824", "#b42934")),
        (top_plate, ("#c2353c", "#f06a43")),
    ):
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0.0, QColor(colors[0]))
        grad.setColorAt(1.0, QColor(colors[1]))
        painter.setPen(QPen(QColor(255, 255, 255, 28), max(2, size // 96)))
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(rect, size * 0.05, size * 0.05)

    grid_pen = QPen(QColor(255, 245, 240, 80), max(2, size // 120))
    painter.setPen(grid_pen)
    painter.setBrush(Qt.NoBrush)
    for factor in (0.33, 0.66):
        x = top_plate.left() + top_plate.width() * factor
        y = top_plate.top() + top_plate.height() * factor
        painter.drawLine(QPointF(x, top_plate.top() + size * 0.03), QPointF(x, top_plate.bottom() - size * 0.03))
        painter.drawLine(QPointF(top_plate.left() + size * 0.03, y), QPointF(top_plate.right() - size * 0.03, y))

    stroke_pen = QPen(QColor("#fff4ec"), max(4, size // 58), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(stroke_pen)
    painter.drawLine(
        QPointF(top_plate.left() + top_plate.width() * 0.18, top_plate.top() + top_plate.height() * 0.82),
        QPointF(top_plate.left() + top_plate.width() * 0.82, top_plate.top() + top_plate.height() * 0.18),
    )

    spark_center = QPointF(top_plate.right() - size * 0.02, top_plate.top() + size * 0.06)
    spark = QPainterPath()
    spark.moveTo(spark_center.x(), spark_center.y() - size * 0.065)
    spark.lineTo(spark_center.x() + size * 0.022, spark_center.y() - size * 0.018)
    spark.lineTo(spark_center.x() + size * 0.068, spark_center.y())
    spark.lineTo(spark_center.x() + size * 0.022, spark_center.y() + size * 0.018)
    spark.lineTo(spark_center.x(), spark_center.y() + size * 0.065)
    spark.lineTo(spark_center.x() - size * 0.022, spark_center.y() + size * 0.018)
    spark.lineTo(spark_center.x() - size * 0.068, spark_center.y())
    spark.lineTo(spark_center.x() - size * 0.022, spark_center.y() - size * 0.018)
    spark.closeSubpath()
    painter.setPen(Qt.NoPen)
    spark_grad = QRadialGradient(spark_center, size * 0.08)
    spark_grad.setColorAt(0.0, QColor("#fff6d9"))
    spark_grad.setColorAt(0.55, QColor("#ffb457"))
    spark_grad.setColorAt(1.0, QColor("#ff6f3c"))
    painter.setBrush(QBrush(spark_grad))
    painter.drawPath(spark)

    painter.end()
    return image


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    image = draw_icon(1024)
    image.save(str(PNG_PATH), "PNG")
    pixmap = QPixmap.fromImage(image)
    pixmap.save(str(ICO_PATH), "ICO")
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
