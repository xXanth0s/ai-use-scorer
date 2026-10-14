import { defineAgent } from 'eve';
import { gateway, wrapLanguageModel } from 'ai';
import { MODEL } from './lib/contract';

export default defineAgent({
  model: wrapLanguageModel({
    model: gateway(MODEL),
    middleware: {
      specificationVersion:'v4',
      transformParams:async({params})=>({...params,maxOutputTokens:Math.min(params.maxOutputTokens??6000,6000)}),
    },
  }),
  modelContextWindowTokens: 1050000,
  reasoning: 'low',
  defaultTools: false,
  tool: false,
  limits: {sessionTimeoutMs:300000,maxInputTokensPerSession:100000,maxOutputTokensPerSession:6000,maxTokenCostUsdPerSession:1},
  build: {externalDependencies:['@neondatabase/serverless']},
});
