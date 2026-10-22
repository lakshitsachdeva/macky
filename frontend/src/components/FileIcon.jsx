const ICON_STYLES = {
  pdf: { bg: 'bg-[#1a0f0f]', text: 'text-[#ef4444]', label: 'PDF' },
  image: { bg: 'bg-[#0f1a0f]', text: 'text-[#22c55e]', label: 'IMG' },
  spreadsheet: { bg: 'bg-[#0f1a10]', text: 'text-[#16a34a]', label: 'XLS' },
  presentation: { bg: 'bg-[#1a0f1a]', text: 'text-[#a855f7]', label: 'PPT' },
  docx: { bg: 'bg-[#0f0f1a]', text: 'text-[#6366f1]', label: 'DOC' },
  text: { bg: 'bg-[#1a1a0f]', text: 'text-[#eab308]', label: 'TXT' },
  default: { bg: 'bg-[#1a1a1a]', text: 'text-[#444]', label: 'FILE' },
}

function FileIcon({ type, size = 'default' }) {
  const icon = ICON_STYLES[type] ?? ICON_STYLES.default
  const boxSize = size === 'small' ? 'h-7 w-7' : 'h-8 w-8'
  const textSize = size === 'small' ? 'text-[7px]' : 'text-[8px]'

  return (
    <div
      aria-hidden="true"
      className={`${boxSize} ${icon.bg} ${icon.text} rounded-lg flex items-center justify-center border border-white/5 shrink-0`}
    >
      <span className={`${textSize} font-semibold tracking-[0.14em] uppercase`}>
        {icon.label}
      </span>
    </div>
  )
}

export default FileIcon
