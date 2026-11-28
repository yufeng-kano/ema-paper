/**
 * A minimal PDF writer for the hand-written fixture documents (docs/pdf-viewer.md § Testing).
 * It writes what it is given and nothing more: a header, the objects in order, a cross-reference
 * table and a trailer. Streams are written uncompressed, so a content stream reads as the
 * operators it holds and the writer needs no deflate.
 */

/** One indirect object. With `stream`, `body` is the stream's dictionary and `/Length` is added. */
export interface PdfObj {
  id: number
  body: string | Uint8Array
  stream?: Uint8Array
}

/** Bytes of a string whose characters are all below 256, one byte each (PDF strings are bytes). */
export function latin1(text: string): Uint8Array {
  const bytes = new Uint8Array(text.length)
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i)
    if (code > 255) throw new Error(`not a byte: U+${code.toString(16)} in ${JSON.stringify(text.slice(0, 40))}`)
    bytes[i] = code
  }
  return bytes
}

/** `<< /Key value … >>` in the order given. Keys are written without their slash. */
export function dict(entries: Record<string, string>): string {
  return `<< ${Object.entries(entries).map(([key, value]) => `/${key} ${value}`).join(' ')} >>`
}

/** A content stream: one operator line per entry. */
export function contentStream(ops: string[]): Uint8Array {
  return latin1(ops.join('\n'))
}

/** The whole file. Object ids must run from 1 without gaps, as the cross-reference table lists them. */
export function pdfDocument(objects: PdfObj[], root: number): Uint8Array {
  const sorted = [...objects].sort((a, b) => a.id - b.id)
  sorted.forEach((obj, index) => {
    if (obj.id !== index + 1) throw new Error(`object ids must run 1..${sorted.length}; found ${obj.id} at ${index + 1}`)
  })
  const chunks: Uint8Array[] = []
  let length = 0
  const push = (bytes: Uint8Array) => {
    chunks.push(bytes)
    length += bytes.length
  }
  // The binary comment line tells readers the file holds 8-bit data (ISO 32000-1, 7.5.2).
  push(latin1('%PDF-1.7\n%âãÏÓ\n'))
  const offsets: number[] = []
  for (const obj of sorted) {
    offsets.push(length)
    push(latin1(`${obj.id} 0 obj\n`))
    if (obj.stream) {
      if (typeof obj.body !== 'string' || !obj.body.endsWith('>>')) throw new Error(`object ${obj.id}: a stream needs a dictionary`)
      push(latin1(`${obj.body.slice(0, -2)}/Length ${obj.stream.length} >>\nstream\n`))
      push(obj.stream)
      push(latin1('\nendstream'))
    } else {
      push(typeof obj.body === 'string' ? latin1(obj.body) : obj.body)
    }
    push(latin1('\nendobj\n'))
  }
  const xref = length
  // Each entry is exactly 20 bytes, its line end included (ISO 32000-1, 7.5.4).
  const entries = offsets.map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')
  push(latin1(`xref\n0 ${sorted.length + 1}\n0000000000 65535 f \n${entries}`))
  push(latin1(`trailer\n${dict({ Size: String(sorted.length + 1), Root: `${root} 0 R` })}\nstartxref\n${xref}\n%%EOF\n`))
  const out = new Uint8Array(length)
  let at = 0
  for (const chunk of chunks) {
    out.set(chunk, at)
    at += chunk.length
  }
  return out
}
