const {
  createRunOncePlugin,
  withDangerousMod,
  withXcodeProject,
} = require('@expo/config-plugins');
const fs = require('fs-extra');
const path = require('path');
const packageJson = require('../package.json');

const GROUP_NAME = 'Assets';
const SOURCE_DIR = 'public';

/**
 * 把 public/（Live2D 资源树：html + js/ + models/ 等）整体复制到 ios/Assets/public，
 * 并递归加入 Xcode 工程资源组，确保 .app bundle 内含完整 public 目录层级，
 * 供 WKWebView 通过 file:// + 当前 bundle 的 Assets 路径加载 live2d/index.html 及其相对资源。
 *
 * 注：expo-custom-assets 的 preserveFolder 模式只扫描 Assets 根目录一层，
 * 无法把 public/live2d/js/*.js 等深层文件加入 Xcode 工程，故自实现递归拷贝。
 */
function withLive2DIosAssets(config) {
  // 1) prebuild 时把 public 递归拷入 ios/Assets/public
  config = withDangerousMod(config, [
    'ios',
    async (modConfig) => {
      const { platformProjectRoot, projectRoot } = modConfig.modRequest;
      const assetsDir = path.join(platformProjectRoot, GROUP_NAME);
      const destDir = path.join(assetsDir, SOURCE_DIR);
      await fs.copy(path.join(projectRoot, SOURCE_DIR), destDir);
      return modConfig;
    },
  ]);

  // 2) 把 Assets/public 下所有文件递归加入 Xcode 工程资源（addResourceFileToGroup + build file）
  config = withXcodeProject(config, async (modConfig) => {
    const { platformProjectRoot } = modConfig.modRequest;
    const project = modConfig.modResults;
    const groupName = GROUP_NAME;

    // 确保 Assets 分组存在
    modConfig.modResults =
      require('@expo/config-plugins').ios.XcodeUtils.ensureGroupRecursively(
        project,
        groupName,
      );

    const assetsDir = path.join(platformProjectRoot, groupName);
    const xcodeUtils = require('@expo/config-plugins').ios.XcodeUtils;

    // 递归收集 Assets/public 下所有文件（保留相对路径）
    const walk = async (dir, base) => {
      const entries = await fs.readdir(dir, { withFileTypes: true });
      for (const entry of entries) {
        const fullPath = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          await walk(fullPath, path.join(base, entry.name));
        } else if (entry.isFile()) {
          const relPath = path.join(base, entry.name);
          // addResourceFileToGroup 以 groupName 相对路径引用；Mac 下 Xcode 用 / 分隔
          xcodeUtils.addResourceFileToGroup({
            filepath: relPath,
            groupName,
            project: modConfig.modResults,
            isBuildFile: true,
            verbose: false,
          });
        }
      }
    };
    await walk(path.join(assetsDir, SOURCE_DIR), SOURCE_DIR);

    return modConfig;
  });

  return config;
}

module.exports = createRunOncePlugin(
  withLive2DIosAssets,
  'with-live2d-ios-assets',
  packageJson.version,
);
