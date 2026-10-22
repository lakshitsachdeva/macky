import { LegalPage } from './shared'

function PrivacyPage() {
  return (
    <LegalPage
      title="privacy policy"
      lede="Recurdo is local by default. This page explains the small amount of information we may collect around the waitlist, plus what changes if you enable optional Gemini features."
      sections={[
        {
          title: 'What data we collect',
          items: [
            'Email addresses that you voluntarily submit through the waitlist form.',
          ],
        },
        {
          title: 'What we do not collect',
          items: [
            'Your files, file contents, or local embeddings by default.',
            'Your search queries inside the app.',
            'Your local embeddings or indexed metadata.',
          ],
        },
        {
          title: 'The app itself',
          paragraphs: [
            'Text extraction, OCR, embeddings, search, and file metadata stay on your device by default.',
            'If you enable Gemini with a GEMINI_API_KEY, optional image description and document enrichment may send image content or document snippets to Google for processing. Search itself remains local.',
          ],
        },
        {
          title: 'Contact',
          paragraphs: ['If you have privacy questions, contact: lakshits.official@gmail.com'],
        },
      ]}
    />
  )
}

export default PrivacyPage
