import type { NextConfig } from 'next';
// demo-data/ holds the bundled read-only reports for HDYAI_DEMO=1; ship it with every server function.
const config:NextConfig={turbopack:{root:process.cwd()},serverExternalPackages:['tsx'],outputFileTracingIncludes:{'/**':['./demo-data/**/*']}};
export default config;
