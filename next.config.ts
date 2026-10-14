import type { NextConfig } from 'next';
const config:NextConfig={turbopack:{root:process.cwd()},serverExternalPackages:['tsx']};
export default config;
