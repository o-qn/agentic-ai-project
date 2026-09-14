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

class NeedsReview(ValueError):
    pass

def contacts(text):
    # Only explicitly labelled names; a filename or first line is not verified identity.
    name = re.search(r'(?im)^(?:name|candidate)\s*:\s*(.{1,120})$',text)
    emails = list(dict.fromkeys(re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',text)))
    return {'name':name.group(1).strip() if name else None,'emails':emails[:10], 'identity_verified':False}

def sections_from_pages(pages):
    result = []
    for page_num,text in pages:
        # Fixed chunks with exact character offsets; no silent truncation.
        for start in range(0,len(text),1600):
            part = text[start:start+1600]
            if part.strip():
                result.append({'id':f'p{page_num}-{start}','location':f'page/part {page_num}, characters {start}–{start+len(part)}','text':part})
    if not result:
        raise NeedsReview('No readable text was extracted')
    if len(result)>160:
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

def extract_inner(path,max_pages,ocr_language='eng'):
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
    return {'sections':sections,'contact':contacts('\n'.join(text for _,text in pages))}

def extract(path,config):
    process=subprocess.Popen([sys.executable,'-m','hr_agent.document_reader',str(path),str(config.max_pages),config.ocr_language],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True,
                             env={**os.environ,'OMP_THREAD_LIMIT':'1'})
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
        print(json.dumps(extract_inner(sys.argv[1],int(sys.argv[2]),sys.argv[3] if len(sys.argv)>3 else 'eng')))
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:400]}))
        sys.exit(1)
