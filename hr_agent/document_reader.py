# Project wrap-up: HR screening application.
"""Bounded extraction in a killable subprocess; preserves every extracted section."""
from pathlib import Path
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
import zipfile
import csv
import io
import signal
from . import chunking

class NeedsReview(ValueError):
    pass

_NAME_LABEL = re.compile(
    r'(?i)^\s*(?:full\s+name|candidate(?:\s+name)?|applicant(?:\s+name)?|name)\s*[:\-–—]\s*(.*?)\s*$'
)
_NAME_NOISE = re.compile(
    r'(?i)\b(?:resume|curriculum\s+vitae|curriculum|vitae|cv|profile|email|e-mail|phone|tel|mobile|linkedin|github|personal\s+details|contact\s+details|skills|experience|education|summary)\b'
)
_NAME_STOPWORDS = {
    'i', 'am', 'a', 'an', 'the', 'this', 'that', 'my', 'only', 'job', 'skill',
    'to', 'and', 'or', 'of', 'for', 'with', 'built', 'worked', 'years',
    'not', 'provided', 'unknown', 'anonymous',
}
_EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
_PHONE = re.compile(r'(?<!\w)(?:\+?\d[\d ()/.\-]{6,}\d)(?!\w)')


def _valid_name(value, *, explicit=False):
    """Return a plausible display name, or None without guessing an identity."""
    value = re.sub(r'\s+', ' ', value).strip(' \t\r\n•|·—–-:')
    if not value:
        return None
    # Contact rows often put email/phone/portfolio details on the same line.
    value = _EMAIL.sub('', value)
    value = _PHONE.sub('', value)
    value = re.split(r'(?i)\s+(?:email|e-mail|phone|tel|mobile|linkedin|github)\s*[:|\-]?\s*', value, maxsplit=1)[0]
    # A name followed by a job title is a common header layout.
    value = re.split(r'\s+[|•·–—-]\s+', value, maxsplit=1)[0].strip()
    if not value:
        return None
    # Preserve explicitly labelled values exactly enough for audit/report
    # escaping. A CV may contain punctuation or a formula-like string; it is
    # still source data, never an instruction, and reports escape it separately.
    if explicit:
        if value.lower().strip(" .'\"") in {'not provided', 'unknown', 'anonymous'} or len(value) > 120:
            return None
        return value
    if _NAME_NOISE.search(value):
        return None
    words = value.split()
    if not explicit and len(words) < 2:
        return None
    if len(words) > 7 or len(value) > 120:
        return None
    for word in words:
        # Unicode-aware checks handle Arabic and other names while rejecting
        # sentences, URLs and numeric identifiers.
        if not word or not word[0].isalpha() or not all(char.isalpha() or char in "'’.-" for char in word):
            return None
    if any(word.lower().strip(".'") in _NAME_STOPWORDS for word in words):
        return None
    return value


def _filename_name(filename):
    if not filename:
        return None
    stem = Path(filename).stem.replace('_', ' ')
    stem = re.sub(r'(?i)\b(?:resume|curriculum\s+vitae|cv|cover\s+letter)\b', ' ', stem)
    stem = re.sub(r'^\s*[A-Za-z]*\d[\w-]*\s*[-–—]\s*', '', stem)
    return _valid_name(stem)


def contacts(text, filename=None):
    """Extract contact metadata without treating it as verified identity.

    Names are checked in labelled fields across the whole extracted document,
    then in the common CV header area, and finally in a name-like filename.
    Filename fallback is deliberately last and still requires a plausible name.
    """
    name_value = None
    lines = text.splitlines()
    # Explicit labels are authoritative for display metadata and may occur below
    # the first page/header (for example in a personal-details table).
    for index, line in enumerate(lines):
        match = _NAME_LABEL.match(line)
        if match:
            name_value = _valid_name(match.group(1), explicit=True)
            if name_value:
                break
        if re.fullmatch(r'(?i)\s*(?:full\s+name|candidate(?:\s+name)?|applicant(?:\s+name)?|name)\s*[:\-–—]?\s*', line):
            if index + 1 < len(lines):
                name_value = _valid_name(lines[index + 1], explicit=True)
                if name_value:
                    break
    if not name_value:
        for line in lines[:20]:
            candidate = re.sub(r'\s+', ' ', line.strip()).strip('•|·—–-')
            # Skip ordinary labelled fields, but accept a compact header such as
            # "Jane Doe | jane@example.com | +1...".
            if ':' in candidate and not re.match(r'(?i)^(?:name|candidate|applicant)\s*:', candidate):
                continue
            name_value = _valid_name(candidate)
            if name_value:
                break
    name_value = name_value or _filename_name(filename)
    emails = list(dict.fromkeys(_EMAIL.findall(text)))
    return {'name':name_value, 'emails':emails[:10], 'identity_verified':False}

def sections_from_pages(pages,strategy=None):
    # Structure-aware chunks with exact character offsets: the concatenated section
    # text reproduces each page exactly (no silent truncation) and recognised heading
    # context is kept out of the quotation text (see hr_agent/chunking.py).
    result = chunking.chunk(pages,strategy or chunking.DEFAULT_STRATEGY)
    if not result:
        raise NeedsReview('No readable text was extracted')
    if len(result)>chunking.SECTION_BUDGET:
        raise NeedsReview('Document exceeds complete-review section budget')
    return result

def ocr_page(path,number,language):
    with tempfile.TemporaryDirectory() as directory:
        prefix=str(Path(directory)/'page')
        subprocess.run(['pdftoppm','-f',str(number),'-l',str(number),'-scale-to','2400','-singlefile','-png',str(path),prefix],
                       check=True,capture_output=True,timeout=25)
        result=subprocess.run(['tesseract',prefix+'.png','stdout','-l',language,'--psm','3','tsv'],
                              check=True,capture_output=True,timeout=25)
        records=list(csv.DictReader(io.StringIO(result.stdout.decode('utf-8')),delimiter='\t'))
        words=[r for r in records if r.get('text','').strip() and float(r['conf'])>=0]
        if not words:
            raise NeedsReview(f'Page {number} is blank/unreadable after OCR')
        confidence=sum(float(r['conf'])*len(r['text']) for r in words)/sum(len(r['text']) for r in words)
        if confidence<70:
            raise NeedsReview(f'Page {number} OCR confidence is below 70%; manual extraction review required')
        lines={}
        for word in words:
            key=(word['block_num'],word['par_num'],word['line_num'])
            lines.setdefault(key,[]).append(word['text'])
        return '\n'.join(' '.join(words) for words in lines.values())

def extract_inner(path,max_pages,ocr_language='eng',filename=None):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix=='.txt':
        text = path.read_text(encoding='utf-8-sig')
        if '\x00' in text:
            raise NeedsReview('Text contains binary content')
        pages = [(1,text)]
    elif suffix=='.docx':
        from docx import Document
        with zipfile.ZipFile(path) as archive:
            if sum(z.file_size for z in archive.infolist())>80*1024*1024:
                raise NeedsReview('DOCX expansion exceeds limit')
            # Text boxes/embedded objects are not reliably extracted by python-docx.
            xml = archive.read('word/document.xml')
            if any(tag in xml for tag in [b'<w:txbxContent',b'<w:altChunk',b'<w:object']):
                raise NeedsReview('DOCX contains unsupported text boxes or embedded content')
        doc = Document(path)
        from docx.oxml.ns import qn
        # Include nested tables, headers and footers through their XML text nodes.
        parts=[]
        containers=[doc.element.body]
        for section in doc.sections:
            containers.extend([section.header._element,section.footer._element,
                               section.first_page_header._element,section.first_page_footer._element,
                               section.even_page_header._element,section.even_page_footer._element])
        visited=set()
        for container in containers:
            if container in visited:
                continue
            visited.add(container)
            for paragraph in container.iter(qn('w:p')):
                parts.append(''.join(node.text or '' for node in paragraph.iter(qn('w:t'))))
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.startswith('word/') and name.endswith('.xml'):
                    xml=archive.read(name)
                    if any(tag in xml for tag in [b'<w:txbxContent',b'<w:altChunk',b'<w:object',b'<w:drawing',b'<w:pict',b'<w:del ',b'<w:ins ']):
                        raise NeedsReview('DOCX has images, text boxes, embedded objects or tracked changes; verify complete extraction manually')
                    if name in {'word/footnotes.xml','word/endnotes.xml'} and b'<w:t' in xml:
                        raise NeedsReview('DOCX includes notes requiring manual extraction review')
        pages = [(1,'\n'.join(parts))]
    elif suffix=='.pdf':
        from pypdf import PdfReader
        pdf = PdfReader(path)
        if pdf.is_encrypted:
            raise NeedsReview('Encrypted PDF requires HR review')
        if len(pdf.pages)>max_pages:
            raise NeedsReview('PDF page count exceeds configured limit')
        pages = []
        for number,page in enumerate(pdf.pages,1):
            text = page.extract_text() or ''
            if len(text.strip())<40 or page.images:
                # Mixed text/image CVs may contain qualifications inside an image. OCR the whole page.
                text=ocr_page(path,number,ocr_language)
                if len(text.strip())<20:
                    raise NeedsReview(f'Page {number} is unreadable after OCR')
            pages.append((number,text))
    else:
        raise NeedsReview('Unsupported CV format; supported: PDF, DOCX, UTF-8 TXT')
    sections = sections_from_pages(pages)
    return {'sections':sections,'contact':contacts('\n'.join(text for _,text in pages), filename or path.name),
            'strategy':chunking.strategy_label(chunking.DEFAULT_STRATEGY)}

def extract(path,config,filename=None):
    tessdata = os.getenv('TESSDATA_PREFIX') or str(Path(__file__).resolve().parent.parent / 'runtime' / 'tessdata')
    child_env = {**os.environ, 'OMP_THREAD_LIMIT':'1'}
    if Path(tessdata).is_dir():
        child_env['TESSDATA_PREFIX'] = tessdata
    process=subprocess.Popen([sys.executable,'-m','hr_agent.document_reader',str(path),str(config.max_pages),config.ocr_language,
                              filename or Path(path).name],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True,
                             env=child_env)
    try:
        stdout,stderr=process.communicate(timeout=min(config.job_seconds,180))
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid,signal.SIGKILL)
        process.communicate()
        raise NeedsReview('Extraction/OCR exceeded its configured time limit') from exc
    result=subprocess.CompletedProcess(process.args,process.returncode,stdout,stderr)
    if result.returncode:
        try:
            message = json.loads(result.stdout).get('error','Extraction failed')
        except ValueError:
            message = 'Extraction process failed or hit its resource limit'
        raise NeedsReview(message)
    return json.loads(result.stdout)

if __name__=='__main__':
    resource.setrlimit(resource.RLIMIT_AS,(1536*1024*1024,1536*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU,(150,150))
    resource.setrlimit(resource.RLIMIT_FSIZE,(80*1024*1024,80*1024*1024))
    try:
        print(json.dumps(extract_inner(sys.argv[1],int(sys.argv[2]),sys.argv[3] if len(sys.argv)>3 else 'eng',
                                       sys.argv[4] if len(sys.argv)>4 else None)))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:400]}))
        sys.exit(1)
