pipeline {
  agent any

  environment {
    NS  = 'tender'
    TAG = "${env.BUILD_NUMBER}"
  }

  stages {
    stage('Checkout') {
      steps { checkout scm }
    }

    stage('Backend tests') {
      steps {
        dir('backend') {
          sh '''
            python3 -m venv .venv
            . .venv/bin/activate
            pip install -q -r requirements.txt pytest
            # Ollama and MongoDB are not available to the pipeline. The live_llm
            # test is the one test that needs them (see backend/README.md).
            pytest -q -m "not live_llm"
          '''
        }
      }
    }

    stage('Frontend tests') {
      steps {
        dir('ui') {
          // npm install, not npm ci: the Windows-generated lockfile fails
          // npm ci on Linux. See the comment in .github/workflows/ci.yml.
          sh 'npm install'
          sh 'npx tsc --noEmit'
          sh 'npm run test -- --run'
        }
      }
    }

    stage('Build images') {
      steps {
        sh "docker build -t tender-backend:${TAG} ./backend"
        sh "docker build -t tender-mcp:${TAG} -f ./backend/Dockerfile.mcp ./backend"
        sh "docker build -t tender-frontend:${TAG} ./ui"
      }
    }

    stage('Load into Minikube') {
      steps {
        // What `minikube image load` does internally, minus the host profile
        // Jenkins doesn't have (2.6). The node's runtime is containerd, so the
        // image goes into its k8s.io namespace, where the kubelet looks.
        sh "docker save tender-backend:${TAG}  | docker exec -i minikube ctr -n k8s.io images import -"
        sh "docker save tender-mcp:${TAG}      | docker exec -i minikube ctr -n k8s.io images import -"
        sh "docker save tender-frontend:${TAG} | docker exec -i minikube ctr -n k8s.io images import -"
      }
    }

    stage('Deploy') {
      steps {
        script { env.DEPLOYED = 'true' }
        // Stamp this build's tag into the manifests (the checkout's copy only)
        // so apply rolls out one revision. Applying the committed :dev tag and
        // then running set image made two, and rollout undo landed on :dev
        // instead of the last good build.
        sh "sed -i -E 's#(image: tender-[a-z]+):dev#\\1:${TAG}#' k8s/*.yaml"
        sh "kubectl apply -n ${NS} -f k8s/"
      }
    }

    stage('Verify') {
      steps {
        sh "kubectl rollout status -n ${NS} deploy/mcp      --timeout=120s"
        sh "kubectl rollout status -n ${NS} deploy/backend  --timeout=120s"
        sh "kubectl rollout status -n ${NS} deploy/frontend --timeout=120s"
        sh """
          kubectl run smoke-${TAG} -n ${NS} --rm -i --restart=Never \
            --image=curlimages/curl -- \
            curl -fsS -m 10 http://backend:8000/api/health > /dev/null
        """
      }
    }
  }

  post {
    failure {
      // The new images are already applied by the time Verify fails, so the
      // broken version is live. Undo is what actually restores service.
      // A failure before Deploy has nothing to undo; undoing then would roll
      // a healthy deployment back to an older revision.
      script {
        if (env.DEPLOYED == 'true') {
          sh "kubectl rollout undo -n ${NS} deploy/backend  || true"
          sh "kubectl rollout undo -n ${NS} deploy/mcp      || true"
          sh "kubectl rollout undo -n ${NS} deploy/frontend || true"
        }
      }
    }
    always {
      sh "docker image prune -f --filter 'until=168h' || true"
    }
  }
}