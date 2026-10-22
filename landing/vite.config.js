import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

function readRequestBody(request) {
  return new Promise((resolve, reject) => {
    if (request.method !== 'POST') {
      resolve('')
      return
    }

    const chunks = []
    request.on('data', (chunk) => chunks.push(chunk))
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    request.on('error', reject)
  })
}

function attachApiHelpers(response) {
  response.status = (code) => {
    response.statusCode = code
    return response
  }

  response.json = (payload) => {
    if (!response.getHeader('Content-Type')) {
      response.setHeader('Content-Type', 'application/json; charset=utf-8')
    }
    response.end(JSON.stringify(payload))
  }

  return response
}

function waitlistDevApiPlugin() {
  const routes = [
    ['/api/waitlist', './api/waitlist.js'],
    ['/api/access-status', './api/access-status.js'],
    ['/api/installer', './api/installer.js'],
    ['/api/admin/requests', './api/admin/requests.js'],
    ['/api/admin/access', './api/admin/access.js'],
  ]

  return {
    name: 'waitlist-dev-api',
    configureServer(server) {
      for (const [route, modulePath] of routes) {
        server.middlewares.use(route, async (request, response) => {
          const apiResponse = attachApiHelpers(response)

          try {
            const rawBody = await readRequestBody(request)
            request.body = rawBody ? JSON.parse(rawBody) : undefined

            const { default: handler } = await import(modulePath)
            await handler(request, apiResponse)
          } catch (error) {
            server.ssrFixStacktrace(error)
            console.error(`Local API error for ${route}`, error)
            if (!response.headersSent) {
              apiResponse.status(500).json({ error: `Local API failed for ${route}.` })
            }
          }
        })
      }
    },
  }
}

export default defineConfig(({ mode }) => {
  const landingEnv = loadEnv(mode, __dirname, '')
  const projectEnv = loadEnv(mode, path.resolve(__dirname, '..'), '')

  for (const [key, value] of Object.entries({ ...projectEnv, ...landingEnv })) {
    if (!(key in process.env)) {
      process.env[key] = value
    }
  }

  return {
    plugins: [react(), waitlistDevApiPlugin()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    build: {
      rollupOptions: {
        input: {
          main: path.resolve(__dirname, 'index.html'),
          admin: path.resolve(__dirname, 'admin.html'),
          download: path.resolve(__dirname, 'download.html'),
        },
      },
    },
  }
})
