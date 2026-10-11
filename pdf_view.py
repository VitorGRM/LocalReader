"""Visualizador de PDF com destaque do período lido por cima da página.

As páginas ficam empilhadas numa cena em coordenadas de pontos do PDF; a
escala da view define o zoom. Só as páginas visíveis (e vizinhas) são
renderizadas, na resolução da tela, e as distantes são descartadas para não
acumular memória em documentos longos.
"""
from bisect import bisect_right

import pymupdf
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
)

from pdf_layout import PdfLayout
from theme import PDF_HIGHLIGHT, PDF_HOVER

PAGE_GAP = 14.0  # pontos entre páginas
SCENE_PAD = 14.0
SIDE_MARGIN_PX = 24
MIN_ZOOM, MAX_ZOOM = 0.4, 5.0
MAX_RENDER_SCALE = 6.0
RENDER_AHEAD_VIEWPORTS = 1.5  # pré-desenha esta distância acima e abaixo da tela
KEEP_VIEWPORTS = 8  # páginas mais longe que isso liberam a memória
CLICK_SLOP_PX = 5
HIGHLIGHT_PAD = 1.2


class _Page:
    def __init__(self, x: float, top: float, width: float, height: float, scene: QGraphicsScene):
        self.x, self.top, self.width, self.height = x, top, width, height
        self.background = scene.addRect(QRectF(x, top, width, height))
        self.background.setBrush(QBrush(Qt.GlobalColor.white))
        self.background.setPen(QPen(Qt.PenStyle.NoPen))
        self.pixmap_item = QGraphicsPixmapItem()
        self.pixmap_item.setPos(x, top)
        self.pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        scene.addItem(self.pixmap_item)
        self.rendered_scale = 0.0

    def clear(self) -> None:
        if self.rendered_scale:
            self.pixmap_item.setPixmap(QPixmap())
            self.rendered_scale = 0.0


class PdfView(QGraphicsView):
    sentence_clicked = pyqtSignal(int)
    navigate_prev = pyqtSignal()
    navigate_next = pyqtSignal()
    toggle_play = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._doc: pymupdf.Document | None = None
        self._layout: PdfLayout | None = None
        self._pages: list[_Page] = []
        self._page_tops: list[float] = []
        self._max_width = 1.0
        self._zoom = 1.0  # relativo a "ajustar à largura"
        self._current = -1
        self._hover: int | None = None
        self._current_items: list[QGraphicsRectItem] = []
        self._hover_items: list[QGraphicsRectItem] = []
        self._press_pos: QPoint | None = None

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_step)
        self.verticalScrollBar().valueChanged.connect(self._schedule_render)
        self.horizontalScrollBar().valueChanged.connect(self._schedule_render)

    # ---------- Documento ----------
    def set_document(self, path: str, layout: PdfLayout) -> None:
        self.close_document()
        self._doc = pymupdf.open(path)
        self._layout = layout
        self._max_width = max((w for w, _ in layout.page_sizes), default=1.0) or 1.0
        top = 0.0
        for width, height in layout.page_sizes:
            x = (self._max_width - width) / 2
            self._pages.append(_Page(x, top, width, height, self._scene))
            self._page_tops.append(top)
            top += height + PAGE_GAP
        total_height = max(top - PAGE_GAP, 1.0)
        self._scene.setSceneRect(
            QRectF(-SCENE_PAD, -SCENE_PAD, self._max_width + 2 * SCENE_PAD, total_height + 2 * SCENE_PAD)
        )
        self._zoom = 1.0
        self._apply_scale(keep_center=False)
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())

    def close_document(self) -> None:
        self._render_timer.stop()
        self._current_items.clear()
        self._hover_items.clear()
        self._scene.clear()
        self._pages.clear()
        self._page_tops.clear()
        self._layout = None
        self._current = -1
        self._hover = None
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def set_background(self, color: QColor) -> None:
        self.setBackgroundBrush(QBrush(color))

    # ---------- Zoom ----------
    def _fit_scale(self) -> float:
        available = max(self.viewport().width() - 2 * SIDE_MARGIN_PX, 100)
        return available / (self._max_width + 2 * SCENE_PAD)

    def _apply_scale(self, keep_center: bool = True) -> None:
        if not self._pages:
            return
        center = self.mapToScene(self.viewport().rect().center()) if keep_center else None
        scale = self._fit_scale() * self._zoom
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setTransform(QTransform.fromScale(scale, scale))
        self.setTransformationAnchor(anchor)
        if center is not None:
            self.centerOn(center)
        self._schedule_render()

    def zoom_by(self, factor: float) -> None:
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        self._zoom = new_zoom
        self._apply_scale()

    def fit_width(self) -> None:
        self._zoom = 1.0
        self._apply_scale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_scale()

    # ---------- Renderização ----------
    def _schedule_render(self, *_args) -> None:
        # Sem reiniciar o timer: durante uma rolagem contínua as páginas vão sendo
        # desenhadas, em vez de esperar a rolagem parar.
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _render_step(self) -> None:
        """Desenha uma página por vez (visíveis primeiro), mantendo a interface fluida."""
        if self._doc is None or not self._pages:
            return
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        height = max(visible.height(), 1.0)
        want_top = visible.top() - height * RENDER_AHEAD_VIEWPORTS
        want_bottom = visible.bottom() + height * RENDER_AHEAD_VIEWPORTS
        keep_top, keep_bottom = visible.top() - height * KEEP_VIEWPORTS, visible.bottom() + height * KEEP_VIEWPORTS
        center = visible.center().y()
        scale = min(self.transform().m11() * self.devicePixelRatioF(), MAX_RENDER_SCALE)

        pending: list[tuple[bool, float, int]] = []
        for index, page in enumerate(self._pages):
            bottom = page.top + page.height
            if bottom < keep_top or page.top > keep_bottom:
                page.clear()
            elif bottom >= want_top and page.top <= want_bottom:
                if abs(page.rendered_scale - scale) > scale * 0.02:
                    on_screen = bottom >= visible.top() and page.top <= visible.bottom()
                    distance = abs((page.top + bottom) / 2 - center)
                    pending.append((not on_screen, distance, index))
        if not pending:
            return
        _, _, index = min(pending)
        self._render_page(index, self._pages[index], scale)
        if len(pending) > 1:
            self._render_timer.start(0)

    def _render_page(self, index: int, page: _Page, scale: float) -> None:
        pix = self._doc[index].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        page.pixmap_item.setPixmap(QPixmap.fromImage(image))
        page.pixmap_item.setScale(1 / scale)
        page.rendered_scale = scale

    # ---------- Destaques ----------
    def _make_items(self, index: int, color: QColor, z: float) -> list[QGraphicsRectItem]:
        if self._layout is None or not 0 <= index < len(self._layout.sentence_rects):
            return []
        items = []
        for page_no, (x0, y0, x1, y1) in self._layout.sentence_rects[index]:
            page = self._pages[page_no]
            rect = QRectF(
                page.x + x0 - HIGHLIGHT_PAD,
                page.top + y0 - HIGHLIGHT_PAD,
                (x1 - x0) + 2 * HIGHLIGHT_PAD,
                (y1 - y0) + 2 * HIGHLIGHT_PAD,
            )
            item = self._scene.addRect(rect)
            item.setBrush(QBrush(color))
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setZValue(z)
            items.append(item)
        return items

    @staticmethod
    def _remove_items(items: list[QGraphicsRectItem]) -> None:
        for item in items:
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
        items.clear()

    def set_current(self, index: int, scroll: bool = True) -> None:
        self._current = index
        self._remove_items(self._current_items)
        self._current_items = self._make_items(index, PDF_HIGHLIGHT, 2)
        if self._hover == index:
            self._set_hover(None)
        if scroll and self._current_items:
            self._ensure_visible(self._current_items[0].rect())

    def _set_hover(self, index: int | None) -> None:
        if index == self._current:
            index = None
        if index == self._hover:
            return
        self._hover = index
        self._remove_items(self._hover_items)
        if index is not None:
            self._hover_items = self._make_items(index, PDF_HOVER, 1)
        cursor = Qt.CursorShape.PointingHandCursor if index is not None else Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(cursor)

    def _ensure_visible(self, rect: QRectF) -> None:
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        margin = visible.height() * 0.12
        if visible.top() + margin <= rect.top() and rect.bottom() <= visible.bottom() - margin:
            return
        # Coloca o período a ~25% do topo, como uma leitura acompanhando o texto.
        target_center_y = rect.top() + visible.height() * 0.25
        self.centerOn(QPointF(visible.center().x(), target_center_y))

    # ---------- Mouse / teclado ----------
    def _sentence_at(self, pos: QPoint) -> int | None:
        if self._layout is None or not self._pages:
            return None
        point = self.mapToScene(pos)
        page_no = bisect_right(self._page_tops, point.y()) - 1
        if page_no < 0:
            return None
        page = self._pages[page_no]
        x, y = point.x() - page.x, point.y() - page.top
        if not (0 <= x <= page.width and 0 <= y <= page.height):
            return None
        return self._layout.sentence_at(page_no, x, y)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._set_hover(self._sentence_at(event.position().toPoint()))

    def leaveEvent(self, event):
        self._set_hover(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        pos = event.position().toPoint()
        moved = (pos - self._press_pos).manhattanLength()
        self._press_pos = None
        if moved <= CLICK_SLOP_PX:
            index = self._sentence_at(pos)
            if index is not None:
                self.sentence_clicked.emit(index)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120
            if steps:
                self.zoom_by(1.1 ** steps)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Up:
            self.navigate_prev.emit()
        elif key == Qt.Key.Key_Down:
            self.navigate_next.emit()
        elif key == Qt.Key.Key_Space:
            self.toggle_play.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()
