"""Exercise actual controller dock methods in an Anki-bundled Qt main window."""
import ast
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_panel_smoke as smoke

QtGui, QtWidgets, StudyPanel, StudyTracker, timezone = smoke._load_panel_types()
from PyQt6.QtCore import Qt, QTimer
from study_companion.panel import PanelToggleButton

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
font_id = QtGui.QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\malgun.ttf')
app.setFont(QtGui.QFont(QtGui.QFontDatabase.applicationFontFamilies(font_id)[0], 9))
window = QtWidgets.QMainWindow()
window.resize(925, 796)
central = QtWidgets.QWidget()
central.setMinimumWidth(600)
window.setCentralWidget(central)
window.form = type('Form', (), {'menuTools': window.menuBar().addMenu('Tools')})()
tree = ast.parse((smoke.ROOT / 'study_companion/addon.py').read_text(encoding='utf-8'))
controller = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Controller')
methods = [node for node in controller.body if isinstance(node, ast.FunctionDef)
           and node.name in ('_build_side_panel', 'set_panel_collapsed', 'refresh_panel')]
scope = dict(mw=window, Qt=Qt, QTimer=QTimer, StudyPanel=StudyPanel,
             PanelToggleButton=PanelToggleButton)
for name in ('QDockWidget', 'QScrollArea', 'QToolButton', 'QStyle', 'QWidget', 'QHBoxLayout'):
    scope[name] = getattr(QtWidgets, name)
exec(compile(ast.Module(body=methods, type_ignores=[]), '<actual dock methods>', 'exec'), scope)
fake = smoke.FakeController(StudyTracker(), smoke.datetime.now(timezone))
fake.ui_state = {}
fake.closed = False
fake.save = lambda: None
import types
fake.action = types.SimpleNamespace(setText=lambda _text: None)
for name in ('_build_side_panel', 'set_panel_collapsed', 'refresh_panel'):
    setattr(fake, name, types.MethodType(scope[name], fake))
fake._build_side_panel()
window.show()
app.processEvents()
fake.set_panel_collapsed(False)
app.processEvents()
refresh_calls = []
fake.panel_body.refresh = lambda: refresh_calls.append(fake.panel.isVisible())
fake.set_panel_collapsed(True)
fake.refresh_panel()
assert refresh_calls == []
fake.set_panel_collapsed(False, persist=False)
fake.refresh_panel()
assert refresh_calls == [True, True]
fake.panel_body.refresh = lambda: None
original = fake.panel.width()
for _ in range(5):
    fake.panel_body.collapse_panel.click()
    app.processEvents()
    assert not fake.panel.isVisible()
    assert fake.panel_expand.isVisible()
    assert central.width() >= window.width() - 5
    fake.panel_expand.click()
    app.processEvents()
    assert fake.panel.isVisible() and not fake.panel_expand.isVisible()
    assert abs(fake.panel.width() - original) <= 2
    assert fake.panel.geometry().right() < window.width()
fake.set_panel_collapsed(True)
window.resize(780, 620)
fake.ui_state['panel_width'] = 4000
fake.panel_expand.click()
app.processEvents()
assert fake.panel.geometry().right() < window.width()
assert fake.panel_scroll.viewport().width() > 0
assert fake.panel_body.collapse_panel.isVisible()
assert fake.panel.titleBarWidget().geometry().width() <= fake.panel.width()
fake.panel_action.trigger()
app.processEvents()
assert not fake.panel.isVisible() and fake.panel_expand.isVisible()
smoke.OUTPUT.mkdir(exist_ok=True)
window.grab().save(str(smoke.OUTPUT / 'dock-collapsed.png'))
fake.panel_expand.click()
app.processEvents()
window.grab().save(str(smoke.OUTPUT / 'dock-expanded.png'))
for theme, bg, fg in (('dark', '#202020', '#eeeeee'), ('light', '#fafafa', '#202020')):
    palette = window.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(bg))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(fg))
    window.setPalette(palette)
    for collapsed in (True, False):
        fake.set_panel_collapsed(collapsed)
        app.processEvents()
        button = fake.panel_expand if collapsed else fake.panel_body.collapse_panel
        assert button.width() == button.height() == 32
        assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
        capture = button.grab().toImage()
        corner = capture.pixelColor(0, 0).lightness()
        contrast = max(abs(capture.pixelColor(x, y).lightness() - corner)
                       for x in range(12, 20) for y in range(10, 23))
        assert contrast > 100, (theme, contrast)
        window.grab().save(str(smoke.OUTPUT / f'dock-{theme}-{collapsed}.png'))
window.close()
print('dock smoke passed: hide/show, repeated restore, narrow window, menu toggle')
