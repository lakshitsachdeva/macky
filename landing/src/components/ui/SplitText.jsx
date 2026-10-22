import { useLayoutEffect, useRef } from 'react'
import gsap from 'gsap'

export default function SplitText({
  text,
  className = '',
  delay = 18,
  duration = 0.6,
  ease = 'power2.out',
  splitType = 'words',
}) {
  const rootRef = useRef(null)
  const parts = splitType === 'chars' ? text.split('') : text.split(' ')

  useLayoutEffect(() => {
    if (!rootRef.current) {
      return undefined
    }

    const ctx = gsap.context(() => {
      const nodes = rootRef.current.querySelectorAll('[data-split]')
      gsap.fromTo(
        nodes,
        {
          opacity: 0,
          y: 10,
          filter: 'blur(10px)',
        },
        {
          opacity: 1,
          y: 0,
          filter: 'blur(0px)',
          duration,
          ease,
          stagger: delay / 1000,
        },
      )
    }, rootRef)

    return () => ctx.revert()
  }, [delay, duration, ease, text])

  return (
    <div ref={rootRef} className={className}>
      {parts.map((part, index) => (
        <span
          key={`${part}-${index}`}
          data-split
          style={{ display: 'inline-block', whiteSpace: 'pre-wrap' }}
        >
          {part}
          {splitType === 'words' && index < parts.length - 1 ? ' ' : ''}
        </span>
      ))}
    </div>
  )
}
