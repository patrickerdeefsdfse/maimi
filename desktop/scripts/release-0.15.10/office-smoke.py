"""Exercise the installed image wheel through the office packages that consume it."""
from io import BytesIO
from tempfile import TemporaryDirectory
from pathlib import Path
from PIL import Image, __version__
from docx import Document
from openpyxl import Workbook,load_workbook
from openpyxl.drawing.image import Image as SheetImage
from pptx import Presentation
from pptx.util import Inches

assert __version__=='13.0.0'
with TemporaryDirectory() as folder:
    root=Path(folder)
    image=root/'fixture.png'
    synthetic=Image.new('RGB',(16,12),(120,152,33))
    synthetic.save(image)
    synthetic.save(root/'fixture.jpg')
    for file in (image,root/'fixture.jpg'):
        with Image.open(file) as loaded:loaded.load();assert loaded.size==(16,12)
    doc=Document()
    doc.add_picture(str(image))
    doc.save(root/'fixture.docx')
    assert len(Document(root/'fixture.docx').inline_shapes)==1
    book=Workbook()
    book.active.add_image(SheetImage(str(image)),'A1')
    book.save(root/'fixture.xlsx')
    reopened=load_workbook(root/'fixture.xlsx')
    assert len(reopened.active._images)==1
    reopened.close()
    slides=Presentation()
    slides.slides.add_slide(slides.slide_layouts[6]).shapes.add_picture(str(image),0,0,width=Inches(1))
    slides.save(root/'fixture.pptx')
    assert len(Presentation(root/'fixture.pptx').slides[0].shapes)==1
