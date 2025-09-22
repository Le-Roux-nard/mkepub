#!/usr/bin/env python3

"""
Create Epub files.

This code was designed to provide a very simple and straight-forward API for
creating epub files, by sacrificing most of the versatility of the format.
"""

###############################################################################
# Module Imports
###############################################################################

import collections
import datetime
import imghdr
import re
import itertools
from typing import Optional
import jinja2
import pathlib
import tempfile
import uuid
import zipfile
import os
import PIL
import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont
from typing import TypedDict, List
import xml.etree.ElementTree as ET


class BookCollectionMetadata(TypedDict):
    name: str
    id: str
    type: str
    number: int


class ContributorMetadata(TypedDict):
    name: str
    role: str


class BookMetadata(TypedDict):
    title: str
    lang: str
    date: str
    collections: List[BookCollectionMetadata]
    creators: List[ContributorMetadata]
    contributors: List[ContributorMetadata]
    subjects: List[str]
    description: str
    rights: str
    cover: str


###############################################################################

def mediatype(name):
    ext = name.split('.')[-1].lower()
    if ext not in ('png', 'jpg', 'jpeg', 'gif', 'svg'):
        raise ValueError('Image format "{}" is not supported.'.format(ext))
    if ext == 'jpg':
        ext = 'jpeg'
    elif ext == 'svg':
        ext = 'svg+xml'
    return 'image/' + ext


def fonttype(name):
    ext = name.split('.')[-1].lower()
    mimetypes = {
        'otf': 'application/font-sfnt',
        'ttf': 'application/font-sfnt',
        'woff': 'font/woff',
        'woff2': 'font/woff2',
    }
    if ext not in mimetypes.keys():
        raise ValueError('Font format "{}" is not supported.'.format(ext))
    return mimetypes[ext]


env = jinja2.Environment(loader=jinja2.PackageLoader('mkepub'))
env.filters['mediatype'] = mediatype
env.filters['fonttype'] = fonttype

###############################################################################


def _extract_default_ns(tag) -> Optional[str]:
    tag_str = str(tag)
    if tag_str.startswith("{") and "}" in tag_str:
        return tag[1:tag_str.find("}")]
    return None


def _text(el):
    return el.text.strip() if el is not None and el.text else None


def _all_text(elems):
    return [e.text.strip() for e in elems if e is not None and e.text and e.text.strip()]


def _first_nonempty(values):
    for v in values:
        if v:
            return v
    return None


def find_opf_package_path(container_bytes: bytes) -> str:
    root = ET.fromstring(container_bytes)
    CONTAINER_NS = {
        "root_namespace": _extract_default_ns(root.tag)
    }
    OPF_MIMETYPE = "application/oebps-package+xml"

    rootfiles = root.findall(
        ".//root_namespace:rootfiles/root_namespace:rootfile", namespaces=CONTAINER_NS)

    if not rootfiles:
        rootfiles = root.findall(".//rootfile")

    chosen = None
    for rf in rootfiles:
        if rf.get("media-type") is OPF_MIMETYPE:
            chosen = rf
            break
    if chosen is None and rootfiles:
        chosen = rootfiles[0]

    if chosen is None:
        raise ValueError("No rootfile found in container.xml")

    full_path = chosen.get("full-path")
    if not full_path:
        raise ValueError(
            "'full-path' attribute missing in rootfile element")

    return full_path


def find_refining_metadata(root_element: ET.ElementTree, root_namespace: str, refined_id: str, refined_property: str):
    temp_namespace = {"root": root_namespace}
    return root_element.find(f"root:meta[@property='{refined_property}'][@refines='#{refined_id}']", temp_namespace)


###############################################################################

Page = collections.namedtuple('Page', 'page_id title children')
Image = collections.namedtuple('Image', 'image_id name')


class Book:
    """EPUB book."""

    def __init__(self, **metadata: BookMetadata):
        """"Create new book."""
        if "title" not in metadata:
            raise AttributeError(
                "No 'title' specified, can't create EPUB3 Book")
        self.metadata: BookMetadata = metadata

        self.tempdir = tempfile.TemporaryDirectory()
        self.root = []
        self.fonts = []
        self.images = []
        self.uuid = uuid.uuid4()
        self._page_id = map('{:04}'.format, itertools.count(1))
        self._image_id = map('{:03}'.format, itertools.count(1))

        self.path = pathlib.Path(self.tempdir.name).resolve()
        for dirname in [
                'EPUB', 'META-INF', 'EPUB/images', 'EPUB/css', 'EPUB/covers']:
            (self.path / dirname).mkdir()

        self.set_stylesheet('')

    @staticmethod
    def read(epub_path: str):
        with zipfile.ZipFile(epub_path, 'r') as archive:
            decompressed_archive_path = tempfile.TemporaryDirectory().name
            archive.extractall(path=decompressed_archive_path)
            container_bytes = archive.read("META-INF/container.xml")
            opf_package_path = find_opf_package_path(container_bytes)

            opf_package_bytes = archive.read(opf_package_path)
            package_root = ET.fromstring(opf_package_bytes)

            PACKAGE_NAMESPACE = {
                "package_root": _extract_default_ns(package_root.tag),
                "dc": "http://purl.org/dc/elements/1.1/",
                "dcterms": "http://purl.org/dc/terms/"
            }

            book_metadatas: BookMetadata = {}

            metadatas = package_root.find(
                "package_root:metadata", PACKAGE_NAMESPACE)

            book_metadatas["title"] = _text(
                metadatas.find("dc:title", PACKAGE_NAMESPACE))
            book_metadatas["lang"] = _text(
                metadatas.find("dc:language", PACKAGE_NAMESPACE))
            book_metadatas["description"] = _text(
                metadatas.find("dc:description", PACKAGE_NAMESPACE))
            book_metadatas["subjects"] = _all_text(
                metadatas.findall("dc:subject", PACKAGE_NAMESPACE))

            # creators = metadatas.findall("dc:creator", PACKAGE_NAMESPACE)
            # contributors = metadatas.findall("dc:contributor", PACKAGE_NAMESPACE)
            def get_contributor_metadata(contrib) -> ContributorMetadata:
                return {
                    "name": _text(contrib),
                    "role": _text(find_refining_metadata(metadatas, PACKAGE_NAMESPACE["package_root"], contrib.get("id"), "role"))
                }

            book_metadatas["creators"] = list(map(get_contributor_metadata, metadatas.findall("dc:creator", PACKAGE_NAMESPACE)))
            book_metadatas["contributors"] = list(map(get_contributor_metadata, metadatas.findall("dc:contributor", PACKAGE_NAMESPACE)))
            
            def get_collection_metadata(collec) -> BookCollectionMetadata:
                return {
                    "id": collec.get("id"),
                    "name": _text(collec),
                    "type": _text(find_refining_metadata(metadatas, PACKAGE_NAMESPACE["package_root"], collec.get("id"), "collection-type")),
                    "number": int(_text(find_refining_metadata(metadatas, PACKAGE_NAMESPACE["package_root"], collec.get("id"), "group-position"))) or 0
                }
            
            book_metadatas["collections"] = list(map(get_collection_metadata, metadatas.findall("package_root:meta[@property='belongs-to-collection']", PACKAGE_NAMESPACE)))

            book_metadatas["date"] = _text(metadatas.find("package_root:meta[@property='dcterms:modified']", PACKAGE_NAMESPACE))
            book_metadatas["rights"] = _text(metadatas.find("package_root:meta[@property='dcterms:rights']", PACKAGE_NAMESPACE))
            
            manifest = package_root.find(
                "package_root:manifest", PACKAGE_NAMESPACE)
            spine = package_root.find(
                "package_root:spine", PACKAGE_NAMESPACE)

            book_cover_archive_path = manifest.find("package_root:item[@properties='cover-image']", PACKAGE_NAMESPACE).get("href")
            book_cover_archive_path = os.sep.join(re.split(r"\\|\/", book_cover_archive_path))
            book_cover_archive_start_dir = os.sep.join(re.split(r"\\|\/", opf_package_path)[:-1])

            book_cover_path = [decompressed_archive_path, book_cover_archive_start_dir, book_cover_archive_path]
            book_cover_path = pathlib.Path(os.sep.join(book_cover_path)).resolve() 

            new_book = Book(**book_metadatas)

            with open(book_cover_path, "rb") as new_book_cover:
                new_book.set_cover(new_book_cover.read())

            archive.close()
            return new_book 


    ###########################################################################
    # Public Methods
    ###########################################################################

    def add_page(self, title, content, parent=None):
        """
        Add a new page.

        The page will be added as a subpage of the parent. If no parent is
        provided, the page will be added to the root of the book.
        """
        page = Page(next(self._page_id), title, [])
        self.root.append(page) if not parent else parent.children.append(page)
        self._write_page(page, content)
        return page

    def add_image(self, name, data):
        """Add image file."""
        self.images.append(Image(next(self._image_id), name))
        self._add_file(pathlib.Path('images') / name, data)

    def add_font(self, name, data):
        """Add font file."""
        self.fonts.append(name)
        self._add_file(pathlib.Path('fonts') / name, data)

    def set_cover(self, data):
        """Set the cover image to the given data."""
        try:
            self.metadata["cover"] = 'cover.' + imghdr.what(None, h=data)
        except:
            self.metadata["cover"] = "cover.jpg"
        self._add_file(pathlib.Path('covers') / self.metadata["cover"], data)
        self._write('cover.xhtml', 'EPUB/cover.xhtml',
                    cover=self.metadata["cover"])

    def generate_cover(self):
        image = PIL.Image.open(
            f"{pathlib.Path(__file__).parent.resolve()}/templates/cover.png")

        width, height = image.size

        title_font = PIL.ImageFont.load_default(60)
        series_font = PIL.ImageFont.load_default(30)
        subseries_font = PIL.ImageFont.load_default(20)

        title = self.metadata["title"].split(",")[1]
        series_name = self.metadata["collection"]["name"]
        volume_number = "Volume " + self.metadata["collection"]["number"]

        draw = PIL.ImageDraw.Draw(image)

        title_bbox = title_font.getbbox(title)
        series_name_bbox = series_font.getbbox(series_name)
        volume_bbox = subseries_font.getbbox(volume_number)

        title_size = (title_bbox[2] - title_bbox[0], title_bbox[3] - title_bbox[1])
        series_name_size = (series_name_bbox[2] - series_name_bbox[0], series_name_bbox[3] - series_name_bbox[1])
        volume_size = (volume_bbox[2] - volume_bbox[0], volume_bbox[3] - volume_bbox[1])

        title_position = ((width - title_size[0]) // 2, height // 5)
        series_name_position = ((width - series_name_size[0]) // 2, height // 2)
        volume_position = ((width - volume_size[0]) // 2, 1.125 * height // 2)

        draw.text(title_position, title, fill="white", font=title_font)
        draw.text(series_name_position, series_name, fill="white", font=series_font)
        draw.text(volume_position, volume_number, fill="white", font=subseries_font)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(
                tmp, self.title.lower().replace(" ", "-") + ".png")
            image.save(path)
            with open(path, "rb") as cover_stream:
                self.set_cover(cover_stream.read())
                cover_stream.close()
            os.unlink(path)
            

    def set_stylesheet(self, data):
        """Set the stylesheet to the given css data."""
        self._add_file(
            pathlib.Path('css') / 'stylesheet.css', data.encode('utf-8'))

    def save(self, filename):
        """Save book to a file."""
        if pathlib.Path(filename).exists():
            raise FileExistsError
        self._write_spine()
        self._write('container.xml', 'META-INF/container.xml')
        self._write_toc()
        with open(str(self.path / 'mimetype'), 'w') as file:
            file.write('application/epub+zip')
        with zipfile.ZipFile(filename, 'w') as archive:
            archive.write(
                str(self.path / 'mimetype'), 'mimetype',
                compress_type=zipfile.ZIP_STORED)
            for file in self.path.rglob('*.*'):
                archive.write(
                    str(file), str(file.relative_to(self.path)),
                    compress_type=zipfile.ZIP_DEFLATED)

    ###########################################################################
    # Private Methods
    ###########################################################################

    def _add_file(self, name, data):
        """Add a file."""
        filepath = self.path / 'EPUB' / name
        if not filepath.parent.exists():
            filepath.parent.mkdir()

        with open(str(filepath), 'wb') as file:
            file.write(data)

    def _write(self, template, path, **data):
        with open(str(self.path / path), 'w', encoding='utf-8') as file:
            file.write(env.get_template(template).render(**data))

    def _write_page(self, page, content):
        """Write the contents of the page into an html file."""
        self._write(
            'page.xhtml', 'EPUB/page{}.xhtml'.format(page.page_id),
            title=page.title, body=content)

    def _write_spine(self):
        # The following lines allow us to setup a default date but it also allows users to specify a publication date and their date will override the default date
        book_metadata: BookMetadata = {
            "date": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
            **self.metadata
        }
        self._write(
            'package.opf',
            'EPUB/package.opf',
            pages=list(self._flatten(self.root)),
            images=self.images,
            fonts=self.fonts,
            uuid=self.uuid,
            **book_metadata
        )

    def _write_toc(self):
        self._write(
            'toc.xhtml', 'EPUB/toc.xhtml', pages=self.root, title=self.metadata["title"])
        self._write(
            'toc.ncx', 'EPUB/toc.ncx',
            pages=self.root, title=self.metadata["title"], uuid=self.uuid)

    def _flatten(self, tree):
        for item in tree:
            yield item
            yield from self._flatten(item.children)
