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
    ext = str(name).split('.')[-1].lower()
    if ext not in ('png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'):
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

def format_path(path:str):
    return str(path).replace("\\", "/")

env = jinja2.Environment(loader=jinja2.PackageLoader('mkepub'))
env.filters['mediatype'] = mediatype
env.filters['fonttype'] = fonttype
env.filters['format_path'] = format_path

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

Page = collections.namedtuple('Page', 'page_id title path stylesheets children')
Image = collections.namedtuple('Image', 'image_id name')
Stylesheet = collections.namedtuple('Stylesheet', 'stylesheet_id path')


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
        self.stylesheets = []
        self.uuid = uuid.uuid4()
        self._page_id = map('{:04}'.format, itertools.count(1))
        self._image_id = map('{:03}'.format, itertools.count(1))
        self._stylesheet_id = map('{:03}'.format, itertools.count(1))
        self.root_folder= "EPUB"
        self.package_file = f"{ self.root_folder}/container.opf"

        self.path = pathlib.Path(self.tempdir.name).resolve()
        # for dirname in [
        #         {self.root_folder}, 'META-INF', f'{self.root_folder}/images', f'{self.root_folder}/css', 'EPUB/covers', 'EPUB/pages']:
        #     (self.path / dirname).mkdir()


    @staticmethod
    def read(epub_path: str):
        with zipfile.ZipFile(epub_path, 'r') as archive:
            decompressed_archive_path = pathlib.Path(tempfile.TemporaryDirectory().name)
            archive.extractall(path=decompressed_archive_path)
            container_bytes = archive.read("META-INF/container.xml")
            opf_package_path = find_opf_package_path(container_bytes)
            epub_root_dir = os.sep.join(re.split(r"\\|\/", opf_package_path)[:-1])

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

            book_cover_archive_path = manifest.find("package_root:item[@properties='cover-image']", PACKAGE_NAMESPACE).get("href")
            book_cover_archive_path = os.sep.join(re.split(r"\\|\/", book_cover_archive_path))

            book_cover_path = decompressed_archive_path / epub_root_dir / book_cover_archive_path

            new_book = Book(**book_metadatas)

            new_book.root_folder = "/".join(re.split(r"\\|\/", opf_package_path)[:-1])
            new_book.package_file = opf_package_path

            with open(book_cover_path, "rb") as new_book_cover:
                new_book.set_cover(new_book_cover.read())

            manifest_items = manifest.findall("package_root:item", PACKAGE_NAMESPACE)
            pages_map = {}
            for item in manifest_items:
                if item.get("properties") == "cover-image":
                    continue
                elif item.get("media-type").startswith("application/") and item.get("media-type") != "application/xhtml+xml":
                    # Should only ignore ToC (will be regenerated dynamically in case of page modification later on)
                    continue
                elif item.get("href") in ["toc.xhtml", "cover.xhtml"]:
                    continue
                else:
                    package_relative_path = item.get("href")
                    abs_path = decompressed_archive_path / epub_root_dir / package_relative_path
                    destination_path = pathlib.Path(package_relative_path)
                    with open(abs_path, "rb") as item_data:
                        # print(f"copying file {package_relative_path} from {abs_path} to {destination_path}")
                        new_book._add_file(destination_path, item_data.read())
                        item_data.close()

                    if item.get("media-type") == "application/xhtml+xml":
                        with open(abs_path, "r", encoding="UTF-8") as page_data:
                            page_id = item.get("id")
                            page_title = re.search(r"(?<=<title>).+?(?=</title>)", page_data.read())[0]
                            page_data.close()
                            pages_map[page_id] = Page(page_id, page_title, package_relative_path, None, [])
                            next(new_book._page_id)
            
            spine = package_root.find("package_root:spine", PACKAGE_NAMESPACE)
            spine_items = spine.findall("package_root:itemref", PACKAGE_NAMESPACE)

            for itemref in spine_items:
                page_id = itemref.get("idref")
                if page_id in pages_map:
                    matching_page_data = pages_map[page_id]
                    new_book.root.append(matching_page_data)

            archive.close()
            return new_book 


    ###########################################################################
    # Public Methods
    ###########################################################################

    def add_page(self, title, content, parent=None, stylesheets=None, custom_path=None):
        """
        Add a new page.

        The page will be added as a subpage of the parent. If no parent is
        provided, the page will be added to the root of the book.
        """
        page_id = next(self._page_id)
        page_path = 'pages/page{}.xhtml'.format(page_id) if not custom_path else custom_path
        page_stylesheets = self.stylesheets if stylesheets is None else stylesheets

        page = Page(page_id, title, page_path, page_stylesheets, [])
        self.root.append(page) if not parent else parent.children.append(page)
        self._write_page(page, content)
        return page

    def add_image(self, name, data, custom_path):
        """Add image file."""
        image_path = pathlib.Path('images') / name if not custom_path else custom_path
        self.images.append(Image(next(self._image_id), image_path))
        self._add_file(image_path, data)

    def add_font(self, name, data):
        """Add font file."""
        self.fonts.append(name)
        self._add_file(pathlib.Path('fonts') / name, data)

    def set_cover(self, data):
        """Set the cover image to the given data."""
        try:
            cover_name = 'cover.' + imghdr.what(None, h=data)
        except:
            cover_name = "cover.jpg"
        self.metadata["cover"] = pathlib.Path('covers') / cover_name
        self._add_file(self.metadata["cover"], data)
        

    def generate_cover(self):
        image = PIL.Image.open(
            f"{pathlib.Path(__file__).parent.resolve()}/templates/cover.png")

        width, height = image.size

        title_font = PIL.ImageFont.load_default(60)
        series_font = PIL.ImageFont.load_default(30)
        subseries_font = PIL.ImageFont.load_default(20)

        title = self.metadata["title"]
        if "collections" in self.metadata and len(self.metadata["collections"]) > 0:
            series_name = self.metadata["collections"][0]["name"] 
            volume_number = "Volume " + self.metadata["collections"][0]["number"]
        else:
            series_name = ""
            volume_number = ""
            
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
                tmp, title.lower().replace(" ", "-") + ".png")
            image.save(path)
            with open(path, "rb") as cover_stream:
                self.set_cover(cover_stream.read())
                cover_stream.close()
            os.unlink(path)
            

    def add_stylesheet(self, name="stylesheet.css", data="", custom_path=None):
        """Set the stylesheet to the given css data."""
        stylesheet_path = str(pathlib.Path('css') / name if not custom_path else custom_path).replace("\\", "/")

        self.stylesheets.append(Stylesheet(next(self._stylesheet_id), stylesheet_path))
        self._add_file(stylesheet_path, data.encode('utf-8'))

    def save(self, filename):
        """Save book to a file."""
        if pathlib.Path(filename).exists():
            raise FileExistsError
        self._write_spine()
        self._write_container()
        self._write_toc()
        if "cover" in self.metadata:
            self._write_cover()
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

    def _create_needed_folders(self, path):
        folderpath = path.parent
        if not folderpath.exists():
            needed_folders = []
            tested_folder = folderpath
            while not tested_folder.exists():
                needed_folders.append(tested_folder)
                tested_folder = tested_folder.parent
            
            needed_folders.reverse()
            for folder in needed_folders:
                folder.mkdir()
        return

    def _add_file(self, name, data):
        """Add a file."""
        filepath = self.path / self.root_folder / name
        self._create_needed_folders(filepath)        

        with open(str(filepath), 'wb') as file:
            file.write(data)

    def _write(self, template, path, **data):
        filepath = self.path / path
        self._create_needed_folders(filepath)
        with open(filepath, 'w', encoding='utf-8') as file:
            file.write(env.get_template(template).render(**data))

    def _write_page(self, page, content):
        """Write the contents of the page into an html file."""

        stylesheets = list(map(lambda x: str(self._find_shortest_path_to_relative_file(page.path, x.path)), page.stylesheets))
        self._write(
            'page.xhtml', f"{self.root_folder}/{page.path}",
            title=page.title, body=content, stylesheets=stylesheets)

    def _write_spine(self):
        # The following lines allow us to setup a default date but it also allows users to specify a publication date and their date will override the default date
        book_metadata: BookMetadata = {
            "date": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
            **self.metadata
        }
        self._write(
            'package.opf',
            self.package_file,
            pages=list(self._flatten(self.root)),
            images=self.images,
            fonts=self.fonts,
            stylesheets=self.stylesheets,
            uuid=self.uuid,
            **book_metadata
        )

    def _write_toc(self):
        self._write(
            'toc.xhtml', f'{self.root_folder}/toc.xhtml', pages=self.root, title=self.metadata["title"], stylesheets=self.stylesheets)
        self._write(
            'toc.ncx', f'{self.root_folder}/toc.ncx',
            pages=self.root, title=self.metadata["title"], uuid=self.uuid, stylesheets=self.stylesheets)        

    def _write_container(self):
        self._write(
            'container.xml', 'META-INF/container.xml',
            package_path=self.package_file)
        
    def _write_cover(self):
        self._write('cover.xhtml', f'{self.root_folder}/pages/cover.xhtml', cover=self.metadata["cover"])

    def _flatten(self, tree):
        for item in tree:
            yield item
            yield from self._flatten(item.children)

    def _find_shortest_path_to_relative_file(self, source_path, destination_path):
        src_dir = pathlib.Path(source_path).resolve(strict=False).parent
        dst_abs = pathlib.Path(destination_path).resolve(strict=False)

        return str(pathlib.Path(os.path.relpath(dst_abs, start=src_dir))).replace(os.sep, "/")
